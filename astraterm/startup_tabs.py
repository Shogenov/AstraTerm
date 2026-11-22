"""Profile-local workspace metadata. Connections always use normal authentication."""
import json

from .core import _atomic_json, _config_lock, validate_session


QUICK_FIELDS = ('protocol', 'name', 'host', 'port', 'username', 'startup_directory',
                'encoding', 'serial_device', 'baud', 'rdp_domain', 'rdp_resolution')


def pane_reference(profile, saved):
    if profile.get('id') in saved:
        return {'session': profile['id']}
    if profile.get('_argv'):
        return None
    return {'profile': {key: profile[key] for key in QUICK_FIELDS if key in profile}}


def resolve_pane(reference, sessions):
    if not isinstance(reference, dict):
        return None
    if 'session' in reference:
        value = reference['session']
        return sessions.get(value) if isinstance(value, str) else None
    profile = reference.get('profile')
    if isinstance(profile, dict):
        try:
            return validate_session({key: profile[key] for key in QUICK_FIELDS if key in profile})
        except ValueError:
            pass
    return None


def load_workspace(path):
    try:
        with open(path, encoding='utf-8') as stream:
            document = json.load(stream)
        if isinstance(document, dict) and document.get('remember') is True:
            return document
    except (OSError, ValueError, RecursionError):
        pass
    return {'remember': False}


def write_workspace(path, remember, tabs=None, active=0):
    document = {'remember': remember, 'tabs': tabs or [], 'active': active}
    with _config_lock(path):
        _atomic_json(path, document)


def project_layout(node, mapping, depth=0):
    """Drop unavailable leaves and translate stored pane indices to new ones."""
    if type(node) is int:
        return mapping.get(node)
    if not isinstance(node, list) or len(node) != 4 or depth >= 256:
        return None
    orientation, ratio, left, right = node
    left, right = (project_layout(branch, mapping, depth + 1) for branch in (left, right))
    if left is None or right is None:
        return left if right is None else right
    if orientation not in ('horizontal', 'vertical') or type(ratio) not in (int, float) or not 0 < ratio < 1:
        return None
    return [orientation, ratio, left, right]


def layout_leaves(node):
    if isinstance(node, list):
        return layout_leaves(node[2]) + layout_leaves(node[3])
    return [node] if node is not None else []
