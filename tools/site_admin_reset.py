#!/usr/bin/env python3
"""Set the site admin password using the existing hosting FTPS account."""
import argparse
import ftplib
import getpass
import hashlib
import io
import json
import os
import sys
import uuid
import warnings

from release import FTPS, deployment_config


def replace_password(client, password, directory='/.astraterm-analytics'):
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, 600000)
    config = json.dumps({'salt': salt.hex(), 'hash': digest.hex()}).encode('ascii')
    client.mkdirs(directory)
    ftp = client.ftp
    ftp.sendcmd('SITE CHMOD 700 ' + directory)
    staged = directory + '/.auth-' + uuid.uuid4().hex
    try:
        ftp.storbinary('STOR ' + staged, io.BytesIO(config))
        ftp.sendcmd('SITE CHMOD 600 ' + staged)
        uploaded = io.BytesIO()
        ftp.retrbinary('RETR ' + staged, uploaded.write)
        if uploaded.getvalue() != config:
            raise ValueError('The uploaded password configuration did not match.')
        # Commit with one rename so readers see either the old hash or the new one.
        ftp.rename(staged, directory + '/auth.json')
    except (ValueError,) + ftplib.all_errors:
        try:
            ftp.delete(staged)
        except ftplib.all_errors:
            pass
        raise


def main():
    config = deployment_config()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default=config.get('host'), help='FTPS hosting hostname')
    parser.add_argument('--user', default=config.get('user'), help='FTPS hosting login')
    args = parser.parse_args()
    if not args.host or not args.user:
        parser.error('Specify the hosting account with --host and --user.')

    with warnings.catch_warnings():
        warnings.simplefilter('error', getpass.GetPassWarning)
        ftp_password = getpass.getpass('Hosting FTP password: ')
        password = getpass.getpass('New site admin password (at least 16 characters): ')
        confirmation = getpass.getpass('Repeat the new site admin password: ')
    if len(password) < 16 or len(password.encode('utf-8')) > 1024 or password != confirmation:
        raise ValueError('Enter matching passwords of at least 16 characters and at most 1024 UTF-8 bytes.')

    client = FTPS(args.host, args.user, ftp_password, '/www/astraterminal.ru')
    try:
        replace_password(client, password)
    finally:
        client.close()
    print('Admin password updated. Sign in at https://astraterminal.ru/admin/')


if __name__ == '__main__':
    try:
        main()
    except (ValueError, EOFError, getpass.GetPassWarning) + ftplib.all_errors as error:
        sys.exit('Admin password reset failed: {}'.format(error))
