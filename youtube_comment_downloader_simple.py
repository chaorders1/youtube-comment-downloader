#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
YouTube评论下载器 - 简化版
一个简单的命令行工具，用于下载YouTube视频的评论，无需YouTube API

python youtube_comment_downloader_simple.py --url "https://www.youtube.com/watch?v=VIDEO_ID" --output comments.json --pretty
python youtube_comment_downloader_simple.py --url "https://www.youtube.com/watch?v=AKzaAvLHtjg" --output comments.json --pretty

可用参数：
--youtubeid 或 -y: YouTube视频ID
--url 或 -u: YouTube视频URL
--output 或 -o: 输出文件名
--pretty 或 -p: 生成格式化的JSON
--limit 或 -l: 最大评论数量
--language 或 -a: 语言设置，例如"zh-CN"
--sort 或 -s: 排序方式（0=热门，1=最新）

"""

from __future__ import print_function

import argparse
import io
import json
import os
import re
import sys
import time

import dateparser
import requests

# 常量定义
YOUTUBE_VIDEO_URL = 'https://www.youtube.com/watch?v={youtube_id}'
YOUTUBE_CONSENT_URL = 'https://consent.youtube.com/save'

USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/79.0.3945.130 Safari/537.36'

SORT_BY_POPULAR = 0
SORT_BY_RECENT = 1

YT_CFG_RE = r'ytcfg\.set\s*\(\s*({.+?})\s*\)\s*;'
YT_INITIAL_DATA_RE = r'(?:window\s*\[\s*["\']ytInitialData["\']\s*\]|ytInitialData)\s*=\s*({.+?})\s*;\s*(?:var\s+meta|</script|\n)'
YT_HIDDEN_INPUT_RE = r'<input\s+type="hidden"\s+name="([A-Za-z0-9_]+)"\s+value="([A-Za-z0-9_\-\.]*)"\s*(?:required|)\s*>'

INDENT = 4


class YoutubeCommentDownloader:
    """YouTube评论下载器类"""

    def __init__(self):
        self.session = requests.Session()
        self.session.headers['User-Agent'] = USER_AGENT
        self.session.cookies.set('CONSENT', 'YES+cb', domain='.youtube.com')

    def ajax_request(self, endpoint, ytcfg, retries=5, sleep=20, timeout=60):
        """执行YouTube AJAX请求"""
        url = 'https://www.youtube.com' + endpoint['commandMetadata']['webCommandMetadata']['apiUrl']

        data = {'context': ytcfg['INNERTUBE_CONTEXT'],
                'continuation': endpoint['continuationCommand']['token']}

        for _ in range(retries):
            try:
                response = self.session.post(url, params={'key': ytcfg['INNERTUBE_API_KEY']}, json=data, timeout=timeout)
                if response.status_code == 200:
                    return response.json()
                if response.status_code in [403, 413]:
                    return {}
            except requests.exceptions.Timeout:
                pass
            time.sleep(sleep)

    def get_comments(self, youtube_id, *args, **kwargs):
        """通过YouTube视频ID获取评论"""
        return self.get_comments_from_url(YOUTUBE_VIDEO_URL.format(youtube_id=youtube_id), *args, **kwargs)

    def get_comments_from_url(self, youtube_url, sort_by=SORT_BY_RECENT, language=None, sleep=.1):
        """通过YouTube URL获取评论"""
        response = self.session.get(youtube_url)

        if 'consent' in str(response.url):
            # 自动同意cookie政策
            params = dict(re.findall(YT_HIDDEN_INPUT_RE, response.text))
            params.update({'continue': youtube_url, 'set_eom': False, 'set_ytc': True, 'set_apyt': True})
            response = self.session.post(YOUTUBE_CONSENT_URL, params=params)

        html = response.text
        ytcfg = json.loads(self.regex_search(html, YT_CFG_RE, default='{}'))
        if not ytcfg:
            return  # 无法提取配置
        if language:
            ytcfg['INNERTUBE_CONTEXT']['client']['hl'] = language

        data = json.loads(self.regex_search(html, YT_INITIAL_DATA_RE, default='{}'))

        item_section = next(self.search_dict(data, 'itemSectionRenderer'), None)
        renderer = next(self.search_dict(item_section, 'continuationItemRenderer'), None) if item_section else None
        if not renderer:
            # 评论被禁用？
            return

        sort_menu = next(self.search_dict(data, 'sortFilterSubMenuRenderer'), {}).get('subMenuItems', [])
        if not sort_menu:
            # 没有排序菜单。可能是社区帖子的请求？
            section_list = next(self.search_dict(data, 'sectionListRenderer'), {})
            continuations = list(self.search_dict(section_list, 'continuationEndpoint'))
            # 重试...
            data = self.ajax_request(continuations[0], ytcfg) if continuations else {}
            sort_menu = next(self.search_dict(data, 'sortFilterSubMenuRenderer'), {}).get('subMenuItems', [])
        if not sort_menu or sort_by >= len(sort_menu):
            raise RuntimeError('无法设置排序')
        continuations = [sort_menu[sort_by]['serviceEndpoint']]

        while continuations:
            continuation = continuations.pop()
            response = self.ajax_request(continuation, ytcfg)

            if not response:
                break

            error = next(self.search_dict(response, 'externalErrorMessage'), None)
            if error:
                raise RuntimeError('服务器返回错误: ' + error)

            actions = list(self.search_dict(response, 'reloadContinuationItemsCommand')) + \
                      list(self.search_dict(response, 'appendContinuationItemsAction'))
            for action in actions:
                for item in action.get('continuationItems', []):
                    if action['targetId'] in ['comments-section',
                                              'engagement-panel-comments-section',
                                              'shorts-engagement-panel-comments-section']:
                        # 处理评论和回复的续接
                        continuations[:0] = [ep for ep in self.search_dict(item, 'continuationEndpoint')]
                    if action['targetId'].startswith('comment-replies-item') and 'continuationItemRenderer' in item:
                        # 处理"显示更多回复"按钮
                        continuations.append(next(self.search_dict(item, 'buttonRenderer'))['command'])

            surface_payloads = self.search_dict(response, 'commentSurfaceEntityPayload')
            payments = {payload['key']: next(self.search_dict(payload, 'simpleText'), '')
                        for payload in surface_payloads if 'pdgCommentChip' in payload}
            if payments:
                # 我们需要将有效载荷键映射到评论ID
                view_models = [vm['commentViewModel'] for vm in self.search_dict(response, 'commentViewModel')]
                surface_keys = {vm['commentSurfaceKey']: vm['commentId']
                                for vm in view_models if 'commentSurfaceKey' in vm}
                payments = {surface_keys[key]: payment for key, payment in payments.items() if key in surface_keys}

            toolbar_payloads = self.search_dict(response, 'engagementToolbarStateEntityPayload')
            toolbar_states = {payload['key']: payload for payload in toolbar_payloads}
            for comment in reversed(list(self.search_dict(response, 'commentEntityPayload'))):
                properties = comment['properties']
                cid = properties['commentId']
                author = comment['author']
                toolbar = comment['toolbar']
                toolbar_state = toolbar_states[properties['toolbarStateKey']]
                result = {'cid': cid,
                          'text': properties['content']['content'],
                          'time': properties['publishedTime'],
                          'author': author['displayName'],
                          'channel': author['channelId'],
                          'votes': toolbar['likeCountNotliked'].strip() or "0",
                          'replies': toolbar['replyCount'],
                          'photo': author['avatarThumbnailUrl'],
                          'heart': toolbar_state.get('heartState', '') == 'TOOLBAR_HEART_STATE_HEARTED',
                          'reply': '.' in cid}

                try:
                    result['time_parsed'] = dateparser.parse(result['time'].split('(')[0].strip()).timestamp()
                except AttributeError:
                    pass

                if cid in payments:
                    result['paid'] = payments[cid]

                yield result
            time.sleep(sleep)

    @staticmethod
    def regex_search(text, pattern, group=1, default=None):
        """正则表达式搜索辅助函数"""
        match = re.search(pattern, text)
        return match.group(group) if match else default

    @staticmethod
    def search_dict(partial, search_key):
        """递归字典搜索辅助函数"""
        stack = [partial]
        while stack:
            current_item = stack.pop()
            if isinstance(current_item, dict):
                for key, value in current_item.items():
                    if key == search_key:
                        yield value
                    else:
                        stack.append(value)
            elif isinstance(current_item, list):
                stack.extend(current_item)


def to_json(comment, indent=None):
    """将评论转换为JSON字符串"""
    comment_str = json.dumps(comment, ensure_ascii=False, indent=indent)
    if indent is None:
        return comment_str
    padding = ' ' * (2 * indent) if indent else ''
    return ''.join(padding + line for line in comment_str.splitlines(True))


def download_comments(youtube_id=None, youtube_url=None, output_file=None, limit=None, 
                     sort_by=SORT_BY_RECENT, language=None, pretty=False):
    """下载YouTube评论的主函数"""
    if not youtube_id and not youtube_url:
        raise ValueError('必须指定YouTube ID或URL')
    
    if not output_file:
        raise ValueError('必须指定输出文件名')

    # 创建输出目录（如果需要）
    if os.sep in output_file:
        outdir = os.path.dirname(output_file)
        if not os.path.exists(outdir):
            os.makedirs(outdir)

    print(f'正在下载 {youtube_id or youtube_url} 的YouTube评论')
    downloader = YoutubeCommentDownloader()
    generator = (
        downloader.get_comments(youtube_id, sort_by, language)
        if youtube_id
        else downloader.get_comments_from_url(youtube_url, sort_by, language)
    )

    count = 1
    with io.open(output_file, 'w', encoding='utf8') as fp:
        sys.stdout.write('已下载 %d 条评论\r' % count)
        sys.stdout.flush()
        start_time = time.time()

        fp.write('{\n')
        if pretty:
            fp.write(' ' * INDENT + '"comments": [\n')
        else:
            fp.write('"comments":[\n')
        
        first_comment = True

        comment = next(generator, None)
        while comment:
            if not first_comment:
                fp.write(',\n')
            else:
                first_comment = False
            
            if pretty:
                comment_str = to_json(comment, indent=INDENT)
                padding = ' ' * (2 * INDENT)
                comment_str = padding + comment_str
            else:
                comment_str = to_json(comment, indent=None)
            
            fp.write(comment_str)
            
            comment = None if limit and count >= limit else next(generator, None)
            sys.stdout.write('已下载 %d 条评论\r' % count)
            sys.stdout.flush()
            count += 1

        fp.write('\n')
        if pretty:
            fp.write(' ' * INDENT + ']\n')
        else:
            fp.write(']\n')
        fp.write('}')
        fp.flush()
    print('\n[{:.2f} 秒] 完成!'.format(time.time() - start_time))
    return count - 1


def main():
    """命令行入口点"""
    parser = argparse.ArgumentParser(description='下载YouTube评论，无需使用YouTube API')
    parser.add_argument('--youtubeid', '-y', help='要下载评论的YouTube视频ID')
    parser.add_argument('--url', '-u', help='要下载评论的YouTube URL')
    parser.add_argument('--output', '-o', help='输出文件名（输出格式为JSON）')
    parser.add_argument('--pretty', '-p', action='store_true', help='更改输出格式为缩进的JSON')
    parser.add_argument('--limit', '-l', type=int, help='限制评论数量')
    parser.add_argument('--language', '-a', type=str, default=None, help='YouTube生成文本的语言（例如：zh-CN）')
    parser.add_argument('--sort', '-s', type=int, default=SORT_BY_RECENT,
                        help='下载热门评论(0)还是最新评论(1)。默认为1')

    args = parser.parse_args()

    try:
        download_comments(
            youtube_id=args.youtubeid,
            youtube_url=args.url,
            output_file=args.output,
            limit=args.limit,
            sort_by=args.sort,
            language=args.language,
            pretty=args.pretty
        )
    except Exception as e:
        print('错误:', str(e))
        sys.exit(1)


if __name__ == '__main__':
    main() 