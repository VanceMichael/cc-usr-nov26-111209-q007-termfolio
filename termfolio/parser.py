import re


class Screen:
    def __init__(self, rows=24, columns=80):
        self.rows, self.columns = rows, columns
        self.cells = [[' '] * columns for _ in range(rows)]
        self.row, self.column = 0, 0
        self.pending = ''

    def feed(self, text):
        text = self.pending + text
        self.pending = ''
        position = 0
        while position < len(text):
            character = text[position]
            if character == '\x1b':
                match = re.match(r'\x1b\[([0-9;]*)([A-Za-z])', text[position:])
                if match is None:
                    self.pending = text[position:]
                    break
                parameters = [int(value or 0) for value in match[1].split(';')]
                command = match[2]
                if command in {'H', 'f'}:
                    self.row = max(0, min(self.rows - 1, (parameters[0] or 1) - 1))
                    self.column = max(0, min(self.columns - 1, ((parameters[1] if len(parameters) > 1 else 1) or 1) - 1))
                elif command == 'J' and parameters[0] == 2:
                    self.cells = [[' '] * self.columns for _ in range(self.rows)]
                elif command == 'K' and parameters[0] == 0:
                    self.cells[self.row][self.column:] = [' '] * (self.columns - self.column)
                elif command != 'm':
                    raise ValueError('屏幕投影不支持该控制序列')
                position += match.end()
                continue
            if character == '\r':
                self.column = 0
            elif character == '\n':
                self.row = min(self.rows - 1, self.row + 1)
            elif character >= ' ':
                self.cells[self.row][self.column] = character
                self.column = min(self.columns - 1, self.column + 1)
            position += 1

    def lines(self):
        return [''.join(row).rstrip() for row in self.cells]


def inspect_document(body):
    screen = Screen()
    screen.feed(body.decode('utf-8'))
    return {'lines': screen.lines(), 'cursor': [screen.row + 1, screen.column + 1],
            'incomplete_control': bool(screen.pending)}
