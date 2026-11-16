"""Plan password migration against the saved session settings without writing either store."""
from collections import defaultdict


def _endpoint(session):
    return (session['protocol'], session['port'], session['username'], session['host'].lower())


def _matches(identifier, record, session, source, endpoints):
    protocol, port, username, host = _endpoint(session)
    if record.get('protocol', 'both') not in ('both', protocol):
        return False
    if identifier in endpoints:
        # The cache key has no separate RDP domain. Do not assume the domain
        # of a saved session is represented by an unqualified cached username.
        return (endpoints[identifier] == (protocol, port, username, host)
                and not (protocol == 'rdp' and session.get('rdp_domain')))
    if not source or source.get('credential_profile') != identifier:
        return False
    return ((source['protocol'], source['port'], source['host'].lower()) == (protocol, port, host)
            and source.get('rdp_domain', '') == session.get('rdp_domain', '')
            and username in ('', record['username']))


def plan_credential_migration(store, source_sessions, credentials, endpoints, existing):
    """Return vault records to save, session copies to update, and counts.

    Match named profiles through the original bookmark ID. Match cached
    passwords by the full effective endpoint. Only refresh an existing
    password when the incoming profile describes the same credential.
    """
    counts = dict(matched=0, linked=0, ambiguous=0, conflicts=0, stale=0, unmatched=0, refreshed=0)
    source_by_id = {session['id']: session for session in source_sessions}
    resolved = [(session, store.resolve_session(session)) for session in store.sessions]
    available, pending = {}, {}
    for identifier, incoming in credentials.items():
        old = existing.get(identifier)
        if old is None:
            available[identifier] = pending[identifier] = incoming
            continue
        source_protocol = incoming.get('protocol', 'both')
        old_protocol = old.get('protocol', 'both')
        if (old['username'] != incoming['username']
                or 'both' not in (old_protocol, source_protocol) and old_protocol != source_protocol):
            counts['conflicts'] += 1
            continue
        if old['password'] != incoming['password']:
            if (old['name'] != incoming['name']
                    or 'protocol' in old and old_protocol != source_protocol):
                counts['conflicts'] += 1
                continue
            pending[identifier] = dict(old, password=incoming['password'])
        match_protocol = old_protocol if source_protocol == 'both' else source_protocol
        available[identifier] = dict(old, protocol=match_protocol)

    # Adding a record would make every saved reference to that ID usable.
    # Block the record if any such reference is no longer demonstrably safe.
    blocked = set()
    for saved, effective in resolved:
        identifier = saved.get('credential_profile')
        if identifier in available and not _matches(identifier, available[identifier], effective,
                                                    source_by_id.get(saved['id']), endpoints):
            blocked.add(identifier)
            counts['stale'] += 1
    for identifier in blocked:
        del available[identifier]

    endpoint_ids = defaultdict(list)
    for identifier, endpoint in endpoints.items():
        if identifier in available:
            endpoint_ids[endpoint].append(identifier)

    session_updates = []
    for saved, effective in resolved:
        if effective['protocol'] not in ('ssh', 'rdp', 'telnet', 'vnc'):
            continue
        identifier = saved.get('credential_profile')
        if identifier:
            if identifier in available:
                counts['matched'] += 1
            elif identifier in credentials:
                counts['unmatched'] += 1
            continue  # Do not replace a credential reference already selected by the user.

        source = source_by_id.get(saved['id'])
        preferred = source.get('credential_profile') if source else None
        if (preferred in available and preferred not in endpoints
                and _matches(preferred, available[preferred], effective, source, endpoints)):
            candidates = [preferred]
        else:
            candidates = [candidate for candidate in endpoint_ids.get(_endpoint(effective), ())
                          if _matches(candidate, available[candidate], effective, source, endpoints)]
        if len(candidates) > 1:
            counts['ambiguous'] += 1
            continue
        if not candidates:
            counts['unmatched'] += 1
            continue

        identifier = candidates[0]
        session_updates.append(dict(saved, credential_profile=identifier,
                                    credential_label=available[identifier]['name']))
        counts['matched'] += 1
        counts['linked'] += 1

    records_to_save = [pending[identifier] for identifier in available if identifier in pending]
    counts['refreshed'] = sum(record['id'] in existing for record in records_to_save)
    return records_to_save, session_updates, counts
