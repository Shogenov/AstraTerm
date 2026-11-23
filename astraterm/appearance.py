"""Profile-local interface preferences shared with the Windows adapter."""
import json
import re


COLORS = {
    'dark': {'accent': '#82d9bd', 'terminal_background': '#101316',
             'terminal_foreground': '#d9e1e8', 'terminal_cursor': '#82d9bd'},
    'light': {'accent': '#246752', 'terminal_background': '#ffffff',
              'terminal_foreground': '#21303a', 'terminal_cursor': '#246752'},
}
COLOR_LABELS = (('accent', 'Акцент кнопок и активной вкладки'),
                ('terminal_background', 'Фон терминала'),
                ('terminal_foreground', 'Текст терминала'),
                ('terminal_cursor', 'Курсор терминала'))
PANELS = ('sidebar', 'files')
HEX_COLOR = re.compile(r'#[0-9a-fA-F]{6}\Z')


def normalize_appearance(value):
    if not isinstance(value, dict):
        value = {}
    colors = value.get('colors')
    overrides = {}
    if isinstance(colors, dict):
        for theme in COLORS:
            if isinstance(colors.get(theme), dict):
                selected = {key: color.lower() for key, color in colors[theme].items()
                            if key in COLORS[theme] and isinstance(color, str) and HEX_COLOR.match(color)}
                if selected:
                    overrides[theme] = selected
    panels = value.get('panels')
    return {'theme': 'light' if value.get('theme') == 'light' else 'dark',
            'terminal_clipboard': value.get('terminal_clipboard') is True,
            'colors': overrides,
            'panels': {key: panels[key] for key in PANELS
                       if isinstance(panels, dict) and type(panels.get(key)) is bool}}


def read_appearance(path):
    try:
        with open(path, encoding='utf-8-sig') as stream:
            return normalize_appearance(json.load(stream))
    except (OSError, ValueError, RecursionError):
        return normalize_appearance(None)


def palette(theme, colors):
    result = COLORS[theme].copy()
    result.update(colors.get(theme, {}))
    return result


def accent_foreground(accent):
    channels = [int(accent[index:index + 2], 16) / 255.0 for index in (1, 3, 5)]
    values = [value / 12.92 if value <= .04045 else ((value + .055) / 1.055) ** 2.4 for value in channels]
    luminance = sum(weight * value for weight, value in zip((.2126, .7152, .0722), values))
    return '#000000' if luminance >= .179 else '#ffffff'
