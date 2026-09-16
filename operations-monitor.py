"""Bounded operational checks; reports contain codes, never subprocess/SMTP errors."""
import argparse
import datetime
import email.message
import email.utils
import json
import os
import pathlib
import re
import shutil
import smtplib
import socket
import ssl
import subprocess
import time
import urllib.parse
import urllib.request


def origin(value):
    parsed = urllib.parse.urlsplit(value)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username
            or parsed.password or parsed.path not in ('', '/') or parsed.query or parsed.fragment):
        raise ValueError('invalid_origin')
    return parsed


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def public_checks(config):
    failures = []
    opener = urllib.request.build_opener(NoRedirect())
    for name, key, route, noindex in [('site', 'APP_ORIGIN', '/', False),
                                    ('api', 'API_ORIGIN', '/v1/ready', True),
                                    ('viewer', 'PREVIEW_VIEWER_ORIGIN', '/healthz', True)]:
        parsed = origin(config[key])
        try:
            with opener.open(config[key].rstrip('/') + route, timeout=5) as response:
                robots = response.headers.get('X-Robots-Tag', '').lower()
                if response.status != 200 or (noindex and 'noindex' not in robots) or (not noindex and 'noindex' in robots):
                    failures.append(name + '_response')
            with socket.create_connection((parsed.hostname, parsed.port or 443), timeout=5) as tcp:
                with ssl.create_default_context().wrap_socket(tcp, server_hostname=parsed.hostname) as tls:
                    expiry = ssl.cert_time_to_seconds(tls.getpeercert()['notAfter'])
                    if expiry - time.time() <= 7 * 86400:
                        failures.append(name + '_certificate_expiring')
        except Exception:
            failures.append(name + '_unreachable')
    return failures


def command(args):
    result = subprocess.run(args, capture_output=True, text=True, timeout=15, check=False)
    if result.returncode:
        raise RuntimeError('command_failed')
    return result.stdout.strip()


def worker_failures(health, now):
    try:
        age = now - datetime.datetime.fromisoformat(health['checked_at'].replace('Z', '+00:00')).timestamp()
        if health['status'] != 'ok' or not 0 <= age <= 60 or health.get('cleanup_failures', 0) or health.get('retention_backlog', 0):
            return ['cleanup_unhealthy']
    except (KeyError, TypeError, ValueError):
        return ['cleanup_unhealthy']
    return []


def local_checks(config):
    failures = []
    compose = json.loads(pathlib.Path(config['COMPOSE_COMMAND_FILE']).read_text())
    if not isinstance(compose, list) or compose[:2] != ['docker', 'compose'] or not all(isinstance(x, str) for x in compose):
        raise ValueError('invalid_compose_command')
    for service in ['postgres', 'api', 'worker', 'gateway']:
        try:
            cid = command(compose + ['ps', '-q', service])
            if not re.fullmatch(r'[a-f0-9]{12,64}', cid):
                raise ValueError('missing_container')
            item = json.loads(command(['docker', 'inspect', cid]))[0]
            if not item['State']['Running'] or item['State'].get('Health', {}).get('Status') != 'healthy':
                failures.append(service + '_unhealthy')
            if service == 'worker':
                health = json.loads(command(['docker', 'exec', cid, 'cat', '/run/ai-deploy/worker-health.json']))
                failures.extend(worker_failures(health, time.time()))
        except Exception:
            failures.append(service + '_unavailable')
    usage = shutil.disk_usage(config['DATA_ROOT'])
    if usage.used / usage.total >= .70:
        failures.append('disk_critical' if usage.used / usage.total >= .80 else 'disk_warning')
    return failures


def send_mail(config, codes):
    host = config.get('SMTP_HOST', '')
    sender, recipient = config.get('SMTP_FROM', ''), config.get('SMTP_TO', '')
    if not re.fullmatch(r'[A-Za-z0-9.-]+', host) or not all(re.fullmatch(r'[^\s<>@]+@[^\s<>@]+\.[^\s<>@]+', x) for x in [sender, recipient]):
        raise ValueError('invalid_mail_config')
    password = (pathlib.Path(config['SMTP_PASSWORD_FILE']).read_text(encoding='utf-8-sig').rstrip('\r\n')
                if config.get('SMTP_PASSWORD_FILE') else config.get('SMTP_PASSWORD', ''))
    if not password or not config.get('SMTP_USER'):
        raise ValueError('missing_mail_credentials')
    if not all(re.fullmatch(r'[a-z_]+', x) for x in codes):
        raise ValueError('invalid_alert_code')
    message = email.message.EmailMessage()
    message['From'], message['To'] = sender, recipient
    message['Subject'] = 'AIfra: ' + ('проверка оповещений' if codes == ['test_alert'] else 'состояние сервиса')
    message['Message-ID'] = email.utils.make_msgid(domain=sender.split('@')[1])
    message.set_content('Автоматическая проверка AIfra.\nРезультат: ' + ', '.join(codes) +
                        '\n\nПроверьте доступность сервиса и состояние сервера.\n'
                        'Пользовательские файлы, IP и секреты в письмо не включены.\n')
    with smtplib.SMTP_SSL(host, int(config.get('SMTP_PORT', 465)), timeout=10, context=ssl.create_default_context()) as smtp:
        smtp.login(config['SMTP_USER'], password)
        refused = smtp.send_message(message)
        if refused:
            raise RuntimeError('mail_not_accepted')
    return message['Message-ID']


def should_notify(state, failures, now):
    previous = state.get('failures', [])
    return ((bool(failures) and (failures != previous or now - state.get('sent_at', 0) >= 1800))
            or (not failures and bool(previous)))


def run(config, mode, test_mail=False):
    failures = public_checks(config)
    if mode == 'local':
        failures.extend(local_checks(config))
    failures = sorted(set(failures))
    state_path = pathlib.Path(config['STATE_FILE']) if config.get('STATE_FILE') else None
    state = json.loads(state_path.read_text()) if state_path and state_path.exists() else {}
    now = time.time()
    mailed = False
    if test_mail or should_notify(state, failures, now):
        try:
            send_mail(config, ['test_alert'] if test_mail else failures or ['recovered'])
            mailed = True
        except Exception:
            return {'ok': False, 'failures': failures + ['mail_delivery_failed'], 'mailAccepted': False}
    if state_path and not test_mail:
        candidate = state_path.with_suffix('.tmp')
        # State contains only bounded status codes and time. Parent is provisioned privately.
        fd = os.open(candidate, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
        with os.fdopen(fd, 'w') as stream:
            json.dump({'failures': failures, 'sent_at': now if mailed else state.get('sent_at', 0)}, stream)
        candidate.replace(state_path)
    return {'ok': not failures, 'failures': failures, 'mailAccepted': mailed}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--config')
    parser.add_argument('--mode', choices=['local', 'external'], required=True)
    parser.add_argument('--test-mail', action='store_true')
    args = parser.parse_args()
    try:
        config = json.loads(pathlib.Path(args.config).read_text()) if args.config else dict(os.environ)
        report = run(config, args.mode, args.test_mail)
    except Exception:
        report = {'ok': False, 'failures': ['monitor_configuration_or_execution_failed'], 'mailAccepted': False}
    print(json.dumps({'checkedAt': datetime.datetime.now(datetime.timezone.utc).isoformat(), **report}))
    raise SystemExit(0 if report['ok'] else 1)
