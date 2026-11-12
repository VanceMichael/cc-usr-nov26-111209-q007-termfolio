import argparse
import datetime
import os
import select
import sys
import termios
import tty


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--update-after-details', type=int, default=0)
    arguments = parser.parse_args()
    descriptor = sys.stdin.fileno()
    original = termios.tcgetattr(descriptor)
    version, page, focus, detail, modal, visits, updated = 'v1', 0, 0, False, False, 0, False
    records = [('箱001完整号', '保温'), ('箱002完整号', ''), ('箱003完整号', '交接需复核'), ('箱004完整号', '易碎'), ('箱005完整号', '上层')]

    def emit(text):
        os.write(sys.stdout.fileno(), text.encode())

    def draw():
        visible = records[page * 2:page * 2 + 3]
        emit('\x1b[2J\x1b[1;1H合作仓库装箱台账')
        if modal:
            emit('\x1b[10;1H台账已更新，空格确认重新读取')
        elif detail:
            record = visible[focus]
            emit('\x1b[5;1H完整箱号 ' + record[0] + '\x1b[7;1H备注 ' + record[1])
            emit('\x1b[22;1Hb返回 q退出')
        else:
            for position, record in enumerate(visible):
                emit(f'\x1b[{position + 4};1H' + ('>' if position == focus else ' ') + record[0][:4] + ' 摘要')
            emit('\x1b[22;1Hn下一页 p上一页 j下移 k上移 o详情 q退出')
        state = '提示' if modal else ('详情' if detail else '列表')
        emit(f'\x1b[24;1H版本{version} {state}页{page + 1} 焦点{focus + 1} 就绪')

    try:
        tty.setraw(descriptor)
        draw()
        while True:
            ready, _, _ = select.select([descriptor], [], [], 0.2)
            if not ready:
                emit('\x1b[2;1H时钟 ' + datetime.datetime.now().strftime('%H:%M:%S') + '\x1b[K')
                continue
            key = os.read(descriptor, 1)
            if not key or key == b'q':
                break
            visible = records[page * 2:page * 2 + 3]
            if modal:
                if key == b' ':
                    modal, version, page, focus = False, 'v2', 0, 0
                    records.append(('箱006完整号', '新到'))
            elif key in {b'm', b's'}:
                emit('\x1b[20;1H禁止访问写入操作')
            elif detail:
                if key == b'b':
                    detail, focus = False, 0
                    visits += 1
                    if arguments.update_after_details and visits >= arguments.update_after_details and not updated:
                        modal, updated = True, True
            elif key == b'n' and page * 2 + 3 < len(records):
                page, focus = page + 1, 0
            elif key == b'p' and page:
                page, focus = page - 1, 0
            elif key == b'j':
                focus = min(focus + 1, len(visible) - 1)
            elif key == b'k':
                focus = max(focus - 1, 0)
            elif key == b'o':
                detail = True
            draw()
    finally:
        termios.tcsetattr(descriptor, termios.TCSADRAIN, original)


if __name__ == '__main__':
    main()
