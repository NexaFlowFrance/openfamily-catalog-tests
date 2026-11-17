"""Real Dokploy API import/deployment test using only disposable synthetic data."""
import base64
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

IMAGE = 'ghcr.io/nexaflowfrance/openfamily:2.2.0'
PINNED_IMAGE = IMAGE + '@sha256:111e6f95ab31082ce5ffab25501ec26142950cb42a6df6be6f2bee5a6c18af0e'
CHECKS = {}
FAILURES = []


class Failure(Exception):
    pass


def command(args, data=None):
    result = subprocess.run(args, input=data, text=True, capture_output=True)
    if result.returncode:
        raise Failure('Local Docker command failed; raw output withheld to avoid exposing auth/configuration.')
    return result.stdout.strip()


def mark(name, passed=True, note=None):
    CHECKS[name] = passed
    print(('PASS: ' if passed else 'FAIL: ') + name, flush=True)
    if not passed:
        FAILURES.append(note or name)


def mask(value):
    if value:
        print('::add-mask::' + str(value), flush=True)


def controller_id():
    ids = command(['docker', 'ps', '-q', '--filter', 'label=com.docker.swarm.service.name=dokploy']).splitlines()
    if len(ids) != 1:
        raise Failure('Expected exactly one healthy native Dokploy controller.')
    guarded = Path('.controller-id').read_text().strip()
    if ids[0] != guarded:
        raise Failure('Controller changed after its egress guard; refusing authentication on an unguarded replacement.')
    return ids[0]


def controller_request(path, method='GET', payload=None, key=False):
    args = ['docker', 'exec', '-i', controller_id(), 'curl', '--silent', '--show-error', '--max-time', '120',
            '-w', '\n%{http_code}', '-b', '/tmp/openfamily-ci.cookies', '-c', '/tmp/openfamily-ci.cookies',
            '-H', 'Content-Type: application/json', '-H', 'Origin: http://localhost:3000']
    if key:
        args += ['-H', '@/tmp/openfamily-ci.headers']
    args += ['-X', method]
    data = None
    if payload is not None:
        args += ['--data-binary', '@-']
        data = json.dumps(payload)
    args += ['http://localhost:3000' + path]
    raw = command(args, data)
    try:
        body, status = raw.rsplit('\n', 1)
        status = int(status)
    except ValueError:
        raise Failure('Controller returned an invalid HTTP response.') from None
    if not 200 <= status < 300:
        raise Failure(f'Native API {path.split("?")[0]} returned HTTP {status}; raw response withheld.')
    try:
        result = json.loads(body)
    except json.JSONDecodeError:
        raise Failure('Controller response was not JSON.') from None
    if isinstance(result, dict) and 'error' in result:
        raise Failure(f'Native API {path.split("?")[0]} returned an error; raw response withheld.')
    return result


def trpc(name, payload=None, post=False):
    path = '/api/trpc/' + name
    if not post and payload is not None:
        path += '?' + urllib.parse.urlencode({'input': json.dumps({'json': payload})})
    result = controller_request(path, 'POST' if post else 'GET', {'json': payload} if post else None)
    try:
        return result['result']['data']['json']
    except (KeyError, TypeError):
        raise Failure('Unexpected tRPC response shape; raw authentication response withheld.') from None


def api(name, payload=None, get=False):
    path = '/api/' + name
    if get and payload:
        path += '?' + urllib.parse.urlencode(payload)
    return controller_request(path, 'GET' if get else 'POST', None if get else payload, key=True)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


HTTP = urllib.request.build_opener(NoRedirect)


def app_request(host, path, method='GET', payload=None, token=None):
    headers = {'Host': host, 'Content-Type': 'application/json'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request('http://127.0.0.1:18080' + path, data=data, headers=headers, method=method)
    try:
        with HTTP.open(request, timeout=10) as response:
            return response.status, response.read().decode('utf8')
    except urllib.error.HTTPError as exc:
        return exc.code, ''
    except (urllib.error.URLError, TimeoutError):
        return 0, ''


def wait_for(fn, label, timeout=300):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        try:
            value = fn()
            if value:
                return value
        except Failure:
            pass
        time.sleep(2)
    raise Failure('Timed out waiting for ' + label + '.')


def app_cid(project):
    result = command(['docker', 'ps', '-q', '--filter', 'label=com.docker.compose.project=' + project,
                      '--filter', 'label=com.docker.compose.service=openfamily'])
    ids = result.splitlines()
    return ids[0] if len(ids) == 1 else None


def data_volume(cid):
    mounts = json.loads(command(['docker', 'inspect', cid, '--format', '{{json .Mounts}}']))
    volumes = [m for m in mounts if m.get('Destination') == '/data' and m.get('Type') == 'volume']
    if len(volumes) != 1:
        raise Failure('Expected one native named data volume.')
    return volumes[0]['Name']


def check_me(host, token, expected_email):
    status, body = app_request(host, '/api/auth/me', token=token)
    if status != 200:
        raise Failure('Persisted OpenFamily session was not accepted.')
    parsed = json.loads(body)
    data = parsed.get('data', {})
    # /me may return the user directly or under a user field across compatible revisions.
    user = data.get('user', data) if isinstance(data, dict) else {}
    if user.get('email') != expected_email:
        raise Failure('Persisted synthetic OpenFamily account was not found.')


def main():
    if os.environ.get('RUNNER_ENVIRONMENT') != 'github-hosted' or os.environ.get('GITHUB_REPOSITORY') != 'NexaFlowFrance/openfamily-catalog-tests':
        raise Failure('This test can run only on its disposable standard GitHub runner.')
    controller = controller_id()
    ports = json.loads(command(['docker', 'service', 'inspect', 'dokploy', '--format', '{{json .Endpoint.Spec.Ports}}']))
    if ports:
        raise Failure('Controller unexpectedly publishes a host port.')
    bindings = json.loads(command(['docker', 'inspect', 'dokploy-traefik', '--format', '{{json .HostConfig.PortBindings}}']))
    if not bindings or any(x.get('HostIp') != '127.0.0.1' for rows in bindings.values() for x in rows):
        raise Failure('Traefik is not restricted to loopback.')
    mark('private_native_controller_and_loopback_proxy')

    fake_email = 'openfamily-ci-' + os.environ.get('GITHUB_RUN_ID', 'local') + '@example.invalid'
    platform_password = secrets.token_urlsafe(32)
    app_password = secrets.token_urlsafe(32)
    mask(platform_password)
    mask(app_password)
    controller_request('/api/auth/sign-up/email', 'POST', {'email': fake_email, 'password': platform_password,
                       'name': 'Test', 'lastName': 'Family'})
    controller_request('/api/auth/sign-in/email', 'POST', {'email': fake_email, 'password': platform_password})
    session = trpc('user.session')
    organization = session['session']['activeOrganizationId']
    key_result = trpc('user.createApiKey', {'name': 'Disposable native test', 'expiresIn': 3600,
                       'rateLimitEnabled': False, 'metadata': {'organizationId': organization}}, post=True)
    key = key_result['key']
    mask(key)
    command(['docker', 'exec', '-i', controller, 'sh', '-c', 'umask 077; cat > /tmp/openfamily-ci.headers'],
            'x-api-key: ' + key + '\n')
    mark('synthetic_bootstrap_and_official_api_authentication')
    api('settings.updateServerIp', {'serverIp': '127.0.0.1'})

    created = api('project.create', {'name': 'OpenFamily native API test', 'description': 'Disposable synthetic integration test'})
    environment = created['environment']['environmentId']
    compose = api('compose.create', {'name': 'OpenFamily', 'environmentId': environment,
                  'composeType': 'docker-compose', 'sourceType': 'raw', 'appName': 'openfamily-native-ci'})
    compose_id = compose['composeId']
    fixture = Path(__file__).resolve().parents[1] / 'fixtures' / 'openfamily'
    raw_yaml = (fixture / 'docker-compose.yml').read_text()
    raw_toml = (fixture / 'template.toml').read_text()
    encoded = base64.b64encode(json.dumps({'compose': raw_yaml, 'config': raw_toml}).encode()).decode()
    api('compose.import', {'composeId': compose_id, 'base64': encoded})
    details = api('compose.one', {'composeId': compose_id}, get=True)
    project_name = details['appName']
    domains = api('domain.byComposeId', {'composeId': compose_id}, get=True)
    if isinstance(domains, dict):
        domains = domains.get('domains', [])
    if len(domains) != 1 or domains[0].get('serviceName') != 'openfamily' or domains[0].get('port') != 3000 or domains[0].get('https'):
        raise Failure('Native template import did not create the expected HTTP application domain.')
    host = domains[0]['host']
    mark('native_base64_compose_and_toml_import_with_generated_domain')

    api('compose.deploy', {'composeId': compose_id, 'freshVolumes': False})
    wait_for(lambda: app_request(host, '/health')[0] == 200, 'native routed application health')
    cid_before = wait_for(lambda: app_cid(project_name), 'native application container')
    volume_before = data_volume(cid_before)
    for path in ('/', '/calendar'):
        status, body = app_request(host, path)
        if status != 200 or '<div id="root">' not in body:
            raise Failure('Native application page or deep URL was not served through the imported domain.')
    mark('native_deployment_http_domain_health_and_deep_url')
    status, body = app_request(host, '/api/auth/register', 'POST', {'email': fake_email, 'password': app_password,
                              'name': 'Synthetic Family', 'timezone': 'Europe/Paris'})
    if status not in (200, 201):
        raise Failure('OpenFamily synthetic registration failed.')
    token = json.loads(body)['data']['token']
    mask(token)
    status, body = app_request(host, '/api/auth/login', 'POST', {'email': fake_email, 'password': app_password})
    if status != 200:
        raise Failure('OpenFamily synthetic login failed.')
    mask(json.loads(body)['data']['token'])
    check_me(host, token, fake_email)
    mark('openfamily_synthetic_registration_login_and_account')

    stopped = False
    try:
        api('compose.stop', {'composeId': compose_id})
        wait_for(lambda: app_request(host, '/health')[0] != 200 and app_cid(project_name) is None,
                 'native API stop', timeout=75)
        mark('native_api_stop')
        stopped = True
    except Failure as exc:
        mark('native_api_stop', False, str(exc))
    if stopped:
        api('compose.start', {'composeId': compose_id})
        wait_for(lambda: app_request(host, '/health')[0] == 200, 'native API start')
        check_me(host, token, fake_email)
        mark('native_api_start_and_persistence')

    # A native redeploy alone need not recreate an unchanged Compose container.
    # Pin the same 2.2.0 image by its public digest to make the config change explicit.
    current = api('compose.one', {'composeId': compose_id}, get=True)
    compose_text = current['composeFile']
    if IMAGE not in compose_text or PINNED_IMAGE in compose_text:
        raise Failure('Cannot safely apply the same-version image pin for the replacement test.')
    changed = compose_text.replace(IMAGE, PINNED_IMAGE, 1)
    api('compose.update', {'composeId': compose_id, 'composeFile': changed})
    old_cid = app_cid(project_name) or cid_before
    api('compose.redeploy', {'composeId': compose_id, 'freshVolumes': False})
    new_cid = wait_for(lambda: (c if (c := app_cid(project_name)) and c != old_cid else None),
                       'native redeployment to a different container')
    wait_for(lambda: app_request(host, '/health')[0] == 200, 'health after replacement')
    if data_volume(new_cid) != volume_before:
        raise Failure('Native replacement did not preserve the data volume.')
    check_me(host, token, fake_email)
    mark('native_redeploy_replaced_container_same_volume_account_and_session')


if __name__ == '__main__':
    try:
        main()
    except Failure as exc:
        mark('test_execution', False, str(exc))
    except Exception as exc:
        # Do not print arbitrary exception values containing authentication/config data.
        mark('test_execution', False, 'Unexpected ' + type(exc).__name__ + '; raw values withheld.')
    report = {'platform': 'Dokploy v0.31.3', 'application': IMAGE, 'method': 'native APIs; no dashboard/UI validation',
              'checks': CHECKS, 'failures': FAILURES, 'success': bool(CHECKS) and not FAILURES}
    print('NATIVE_TEST_REPORT ' + json.dumps(report, sort_keys=True), flush=True)
    summary = os.environ.get('GITHUB_STEP_SUMMARY')
    if summary:
        with open(summary, 'a') as stream:
            stream.write('## OpenFamily / Dokploy native API test\n\n')
            stream.write('Dokploy v0.31.3; OpenFamily 2.2.0. API test only, no visual dashboard, public DNS or HTTPS validation.\n\n')
            for name, passed in CHECKS.items():
                stream.write('- ' + ('PASS' if passed else 'FAIL') + ': ' + name + '\n')
            for failure in FAILURES:
                stream.write('- Detail: ' + failure + '\n')
    sys.exit(0 if report['success'] else 1)
