"""Sort SFTP rows using the numeric values behind the visible cells."""
import re


def sort_rows(rows, key='name', descending=False):
    def name(row):
        return [int(part) if index % 2 else part.casefold()
                for index, part in enumerate(re.split(r'([0-9]+)', row[1]))]

    def value(row):
        if key == 'name':
            return name(row)
        return row[11][{'size': 0, 'permissions': 1, 'created': 2, 'mtime': 3}[key]]

    result = []
    for directory in (True, False):
        group = sorted((row for row in rows if row[4] == directory), key=name)
        known = [row for row in group if value(row) is not None]
        result.extend(sorted(known, key=value, reverse=descending))
        result.extend(row for row in group if value(row) is None)
    return result
