#!/usr/bin/env python3
"""Register Telegram's native command menu for the paired private chat."""
import json
import bridge

COMMANDS = [
    {'command': 'projects', 'description': '点选项目，可加关键词搜索'},
    {'command': 'sessions', 'description': '选择会话，可翻页或加关键词搜索'},
    {'command': 'current', 'description': '查看当前项目、会话和模型'},
    {'command': 'last', 'description': '补看当前会话最近一次结果'},
    {'command': 'new', 'description': '开始新会话'},
    {'command': 'status', 'description': '查看本机正在运行的任务'},
    {'command': 'cancel', 'description': '取消待提交任务或停止 CLI 任务'},
    {'command': 'health', 'description': '检查机器人连接和通知状态'},
    {'command': 'steer', 'description': '向原会话正在执行的任务补充要求'},
    {'command': 'stop', 'description': '停止原会话当前回合'},
    {'command': 'settings', 'description': '调整原会话模型、推理强度和速度'},
    {'command': 'usage', 'description': '查看额度、重置时间和上下文用量'},
    {'command': 'help', 'description': '查看使用说明'},
]


def main():
    cfg = bridge.config()
    api = bridge.API(cfg)
    scope = {'type': 'chat', 'chat_id': cfg['chat_id']}
    for language in ('', 'zh'):
        api.call('setMyCommands', commands=COMMANDS, scope=scope, language_code=language)
        actual = api.call('getMyCommands', scope=scope, language_code=language)
        if actual != COMMANDS:
            raise RuntimeError('Command verification failed')
    api.call('setChatMenuButton', chat_id=cfg['chat_id'], menu_button={'type': 'commands'})
    menu = api.call('getChatMenuButton', chat_id=cfg['chat_id'])
    if menu.get('type') != 'commands':
        raise RuntimeError('Menu verification failed')
    print(json.dumps({'commands': [c['command'] for c in COMMANDS], 'menu': menu,
                      'verified': True}, ensure_ascii=False))


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        print('Menu configuration failed:', type(e).__name__)
        if isinstance(e, bridge.Rejected):
            print('Telegram error code:', e.code)
        raise SystemExit(1)
