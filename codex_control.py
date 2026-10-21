"""Bounded, one-shot access to an already running Codex app server.

Controls use the Desktop's existing owner or official ``app-server proxy``.
A separate one-shot app server is restricted to model catalog and usage reads;
it cannot start or resume a thread.  No mutation is retried, and no writer is
replaced when a control capability is unavailable.
"""

import json
import math
import os
from pathlib import Path
import selectors
import socket
import struct
import subprocess
import time
import uuid
import stat


DEFAULT_CODEX = '/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex'
READ_METHODS = frozenset({
    'thread/read', 'thread/loaded/list', 'model/list', 'account/rateLimits/read',
})
WRITE_METHODS = frozenset({'turn/steer', 'turn/interrupt', 'thread/settings/update'})
MAX_REQUEST_BYTES = 64 * 1024
MAX_OUTPUT_BYTES = 8 * 1024 * 1024
UNSET = object()


class ControlError(Exception):
    """A safe error classification; server text and stderr are never exposed."""

    def __init__(self, code, *, uncertain=False, rpc_code=None):
        self.code = code
        self.uncertain = bool(uncertain)
        self.rpc_code = rpc_code
        super().__init__(code)


def _identifier(value):
    if not isinstance(value, str) or not value or len(value) > 256:
        raise ControlError('invalid_request')
    if any(ord(char) < 32 for char in value):
        raise ControlError('invalid_request')
    return value


class ProxyClient:
    """Each call connects, initializes, performs exactly one RPC, and closes.

    ``sock`` may select an existing control socket.  It is never created here.
    The default CLI socket resolution honors ``CODEX_HOME`` in the environment.
    All I/O, initialization included, shares one deadline.
    """

    def __init__(self, codex=None, sock=None, timeout=6.0):
        self.codex = codex or os.environ.get('CODEX_CLI', DEFAULT_CODEX)
        self.sock = os.fspath(sock) if sock is not None else None
        if not isinstance(timeout, (int, float)) or not math.isfinite(timeout):
            raise ValueError('timeout must be finite')
        if not 0 < timeout <= 30:
            raise ValueError('timeout must be between zero and thirty seconds')
        self.timeout = float(timeout)

    def rpc(self, method, params=None):
        if method not in READ_METHODS | WRITE_METHODS:
            raise ControlError('unsupported_method')
        if params is not None and not isinstance(params, dict):
            raise ControlError('invalid_request')
        # Encode before spawning, so invalid input has no transport side effect.
        try:
            request = self._encode({'id': 2, 'method': method, 'params': params or {}})
        except (TypeError, ValueError, UnicodeError):
            raise ControlError('invalid_request') from None
        if len(request) > MAX_REQUEST_BYTES:
            raise ControlError('invalid_request')
        args = self._command(method)
        deadline = time.monotonic() + self.timeout
        try:
            process = subprocess.Popen(args, stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE,
                                       stderr=subprocess.DEVNULL, bufsize=0)
        except (OSError, ValueError):
            raise ControlError('unavailable') from None
        mutation = method in WRITE_METHODS
        submitted = False
        try:
            with selectors.DefaultSelector() as selector:
                os.set_blocking(process.stdin.fileno(), False)
                os.set_blocking(process.stdout.fileno(), False)
                selector.register(process.stdout, selectors.EVENT_READ)
                exchange = _Exchange(process, selector, deadline)
                exchange.write(self._encode({
                    'id': 1, 'method': 'initialize', 'params': {
                        'clientInfo': {'name': 'codex_telegram_bridge', 'version': '1.0'},
                        'capabilities': {'experimentalApi': True},
                    },
                }))
                exchange.response(1)
                exchange.write(self._encode({'method': 'initialized'}))
                # Any bytes written may reach the server even if a later write
                # or response fails.  Never turn uncertainty into an automatic
                # repeat of steer, interrupt, or settings update.
                submitted = True
                exchange.write(request)
                result = exchange.response(2)
                if not isinstance(result, dict):
                    raise ControlError('protocol')
                return result
        except ControlError as error:
            if error.code != 'rejected' and mutation and submitted:
                error.uncertain = True
            raise
        except (OSError, ValueError):
            raise ControlError('disconnected', uncertain=mutation and submitted) from None
        finally:
            self._close(process)

    @staticmethod
    def _encode(value):
        return (json.dumps(value, separators=(',', ':'), ensure_ascii=False,
                           allow_nan=False) + '\n').encode('utf-8')

    def _command(self, method):
        args = [self.codex, 'app-server', 'proxy']
        if self.sock is not None:
            args += ['--sock', self.sock]
        return args

    @staticmethod
    def _close(process):
        # Terminate only this proxy.  The app-server is an independent process.
        for stream in (process.stdin, process.stdout):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
        try:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=0.5)
        except OSError:
            pass

    def read_thread(self, thread, include_turns=False):
        return self.rpc('thread/read', {'threadId': _identifier(thread),
                                       'includeTurns': bool(include_turns)})

    def loaded_threads(self, cursor=None, limit=100):
        return self.rpc('thread/loaded/list', {'cursor': cursor, 'limit': limit})

    def steer(self, thread, turn, text, request_id=None):
        if not isinstance(text, str) or not text.strip():
            raise ControlError('invalid_request')
        params = {'threadId': _identifier(thread), 'expectedTurnId': _identifier(turn),
                  'input': [{'type': 'text', 'text': text}]}
        if request_id is not None:
            params['clientUserMessageId'] = _identifier(request_id)
        result = self.rpc('turn/steer', params)
        if not isinstance(result,dict) or result.get('turnId') != turn:
            raise ControlError('protocol',uncertain=True)
        return result

    def interrupt(self, thread, turn):
        return self.rpc('turn/interrupt', {'threadId': _identifier(thread),
                                          'turnId': _identifier(turn)})

    def update_settings(self, thread, model=None, effort=None, service_tier=UNSET):
        params = {'threadId': _identifier(thread)}
        if model is not None:
            params['model'] = _identifier(model)
        if effort is not None:
            params['effort'] = _identifier(effort)
        if service_tier is not UNSET:
            params['serviceTier'] = None if service_tier is None else _identifier(service_tier)
        if len(params) == 1:
            raise ControlError('invalid_request')
        # No cwd, permissions, provider, approval, or instructions are written.
        return self.rpc('thread/settings/update', params)

    def models(self, cursor=None):
        return self.rpc('model/list', {'cursor': cursor, 'limit': 100,
                                       'includeHidden': False})

    def rate_limits(self):
        return self.rpc('account/rateLimits/read', {'excludeResetCreditDetails': True})


class CatalogClient(ProxyClient):
    """A short-lived read-only app server for account usage and model catalog.

    No thread is started or resumed, and no inference request is permitted.
    Desktop mutations must always use its existing owner via DesktopClient.
    """

    def rpc(self, method, params=None):
        if method not in {'model/list', 'account/rateLimits/read'}:
            raise ControlError('unsupported_method')
        return super().rpc(method, params)

    def _command(self, method):
        return [self.codex, 'app-server', '--stdio']


class DesktopClient:
    """Follower-only access to the existing Desktop owner over its local IPC.

    The framing and method versions match the installed Desktop's IPC client.
    Connections never register ownership, subscribe to conversation history,
    execute unknown methods, reconnect, or start another process.
    """

    def __init__(self, sock=None, timeout=6.0):
        base = Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex')))
        self.sock = os.fspath(sock) if sock is not None else str(base / 'ipc' / 'ipc.sock')
        if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 < timeout <= 30:
            raise ValueError('timeout must be between zero and thirty seconds')
        self.timeout = float(timeout)

    def find_owner(self, thread):
        with _DesktopSession(self.sock, self.timeout) as session:
            return session.owner(_identifier(thread))

    def read_settings(self, thread):
        thread = _identifier(thread)
        with _DesktopSession(self.sock, self.timeout) as session:
            owner = session.owner(thread)
            response = session.request('thread-follower-read-model-settings',
                                       {'conversationId': thread}, version=1, target=owner)
            settings = response['result'].get('settings')
            if not isinstance(settings, dict):
                raise ControlError('not_loaded')
            return settings

    def interrupt(self, thread, turn):
        thread, turn = _identifier(thread), _identifier(turn)
        with _DesktopSession(self.sock, self.timeout) as session:
            owner = session.owner(thread)
            response = session.request('thread-follower-interrupt-turn', {
                'conversationId': thread, 'mode': 'user-stop', 'expectedTurnId': turn,
            }, version=4, target=owner, mutation=True)
            result = response['result']
            if result.get('ok') is not True:
                raise ControlError('protocol', uncertain=True)
            if result.get('interruptedTurnId') != turn:
                raise ControlError('stale_turn')
            # Goal pause failures can follow a successful exact-turn interrupt.
            return {'ok': True, 'interruptedTurnId': turn,
                    'goalPauseFailed': bool(result.get('goalPauseError'))}

    def steer(self, thread, turn, text, request_id=None):
        """Steer the original thread's active execution, without client retry.

        Installed Desktop IPC does not expose an atomic expected-turn guard.
        The caller must verify ``turn`` just before this call.  The Desktop may
        process the input against a successor active turn in the same thread;
        the accepted turn id is returned when the owner supplies it.
        """
        thread, turn = _identifier(thread), _identifier(turn)
        if not isinstance(text, str) or not text.strip():
            raise ControlError('invalid_request')
        message_id = _identifier(request_id) if request_id is not None else str(uuid.uuid4())
        restore = {'id': message_id, 'text': text, 'createdAt': int(time.time() * 1000),
                   'context': {'prompt': text, 'addedFiles': [], 'fileAttachments': [],
                               'ideContext': None, 'imageAttachments': [],
                               'workspaceRoots': []}}
        with _DesktopSession(self.sock, self.timeout) as session:
            owner = session.owner(thread)
            response = session.request('thread-follower-steer-turn', {
                'conversationId': thread, 'input': [{'type': 'text', 'text': text,
                                                     'text_elements': []}],
                'restoreMessage': restore, 'clientUserMessageId': message_id,
            }, version=1, target=owner, mutation=True)
            result = response['result'].get('result')
            if not isinstance(result, dict):
                raise ControlError('protocol', uncertain=True)
            accepted = result.get('turnId')
            if not isinstance(accepted,str) or not accepted.strip():
                raise ControlError('protocol', uncertain=True)
            return {'turnId': accepted, 'requestedTurnId': turn,
                    'atomicTurnGuard': False}

    def update_settings(self, thread, model=None, effort=None, expected=None,
                        service_tier=UNSET):
        thread = _identifier(thread)
        settings = {}
        if model is not None:
            settings['model'] = _identifier(model)
        if effort is not None:
            settings['effort'] = _identifier(effort)
        if service_tier is not UNSET:
            settings['serviceTier'] = None if service_tier is None else _identifier(service_tier)
        if not settings:
            raise ControlError('invalid_request')
        condition = None
        if expected is not None:
            if not isinstance(expected, dict):
                raise ControlError('invalid_request')
            condition = {'ifModelEquals': expected.get('model'),
                         'ifEffortEquals': expected.get('reasoningEffort')}
        with _DesktopSession(self.sock, self.timeout) as session:
            owner = session.owner(thread)
            response = session.request('thread-follower-update-thread-settings', {
                'conversationId': thread, 'threadSettings': settings,
                'activeTurnId': None, 'condition': condition,
            }, version=2, target=owner, mutation=True)
            if response['result'].get('applied') is False:
                raise ControlError('stale_settings')
            if response['result'].get('applied') is not True:
                raise ControlError('protocol', uncertain=True)
            return {'applied': True}


class Client:
    """Runtime facade; existing Desktop IPC wins when it is available.

    A failed Desktop mutation never falls back to a different transport.  Usage
    and catalog reads use the shared proxy when present, otherwise the narrowly
    scoped CatalogClient.  Thread reads are available only on the shared proxy.
    """

    def __init__(self, codex=None, sock=None, timeout=6.0, desktop_sock=None):
        self.proxy = ProxyClient(codex=codex, sock=sock, timeout=timeout)
        self.desktop = DesktopClient(sock=desktop_sock, timeout=timeout)
        self.catalog = CatalogClient(codex=codex, timeout=timeout)
        self.proxy_explicit = sock is not None

    def _desktop_available(self):
        if self.proxy_explicit:
            return False
        try:
            return stat.S_ISSOCK(os.stat(self.desktop.sock).st_mode)
        except OSError:
            return False

    def read_settings(self, thread):
        if self._desktop_available():
            return self.desktop.read_settings(thread)
        response = self.proxy.read_thread(thread)
        value = response.get('thread')
        if not isinstance(value, dict) or value.get('id') != thread:
            raise ControlError('protocol')
        return {key: value.get(key) for key in ('model', 'reasoningEffort',
                                               'modelProvider', 'serviceTier')}

    def read_thread(self, thread, include_turns=False):
        return self.proxy.read_thread(thread, include_turns=include_turns)

    def loaded_threads(self, cursor=None, limit=100):
        return self.proxy.loaded_threads(cursor=cursor, limit=limit)

    def steer(self, thread, turn, text, request_id=None):
        backend = self.desktop if self._desktop_available() else self.proxy
        return backend.steer(thread, turn, text, request_id=request_id)

    def interrupt(self, thread, turn):
        backend = self.desktop if self._desktop_available() else self.proxy
        return backend.interrupt(thread, turn)

    def update_settings(self, thread, model=None, effort=None, service_tier=UNSET,
                        expected=None):
        if self._desktop_available():
            return self.desktop.update_settings(thread, model=model, effort=effort,
                                                service_tier=service_tier, expected=expected)
        return self.proxy.update_settings(thread, model=model, effort=effort,
                                          service_tier=service_tier)

    def _catalog_backend(self):
        if self.proxy_explicit:
            return self.proxy
        base = Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex')))
        path = Path(self.proxy.sock) if self.proxy.sock else base / 'app-server-control' / 'app-server-control.sock'
        return self.proxy if path.exists() else self.catalog

    def models(self, cursor=None):
        return self._catalog_backend().models(cursor=cursor)

    def rate_limits(self):
        return self._catalog_backend().rate_limits()


class _DesktopSession:
    """One initialized, deadline-bounded IPC session with no request handlers."""

    def __init__(self, path, timeout):
        self.path = path
        self.deadline = time.monotonic() + timeout
        self.socket = None
        self.client_id = 'initializing-client'
        self.read_bytes = 0

    def __enter__(self):
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.socket = connection
        try:
            connection.settimeout(self._remaining())
            connection.connect(self.path)
            response = self.request('initialize', {'clientType': 'codex-telegram-bridge'})
            self.client_id = _identifier(response['result'].get('clientId'))
            return self
        except (OSError, ControlError):
            connection.close()
            raise ControlError('unavailable') from None

    def __exit__(self, *args):
        if self.socket is not None:
            self.socket.close()

    def _remaining(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise ControlError('timeout')
        return remaining

    def _send(self, value):
        raw = json.dumps(value, separators=(',', ':'), ensure_ascii=False,
                         allow_nan=False).encode('utf-8')
        if not 0 < len(raw) <= MAX_REQUEST_BYTES:
            raise ControlError('invalid_request')
        self.socket.settimeout(self._remaining())
        self.socket.sendall(struct.pack('<I', len(raw)) + raw)

    def _read_exact(self, count):
        data = bytearray()
        while len(data) < count:
            self.socket.settimeout(self._remaining())
            chunk = self.socket.recv(count - len(data))
            if not chunk:
                raise ControlError('disconnected')
            self.read_bytes += len(chunk)
            if self.read_bytes > MAX_OUTPUT_BYTES:
                raise ControlError('protocol')
            data.extend(chunk)
        return data

    def _read(self):
        count = struct.unpack('<I', self._read_exact(4))[0]
        if not 0 < count <= MAX_OUTPUT_BYTES:
            raise ControlError('protocol')
        try:
            result = json.loads(self._read_exact(count))
        except (ValueError, UnicodeError):
            raise ControlError('protocol') from None
        if not isinstance(result, dict):
            raise ControlError('protocol')
        return result

    def owner(self, thread):
        response = self.request('thread-owner-discovery', {
            'hostId': 'local', 'conversationId': thread,
        }, version=1)
        owner = response.get('handledByClientId')
        if not isinstance(owner, str) or not owner:
            raise ControlError('not_loaded')
        return owner

    def request(self, method, params, version=0, target=None, mutation=False):
        # Internal allowlist cannot be widened by a public rpc() argument.
        if method not in {'initialize', 'thread-owner-discovery',
                          'thread-follower-read-model-settings',
                          'thread-follower-steer-turn',
                          'thread-follower-interrupt-turn',
                          'thread-follower-update-thread-settings'}:
            raise ControlError('unsupported_method')
        request_id = str(uuid.uuid4())
        envelope = {'type': 'request', 'requestId': request_id,
                    'sourceClientId': self.client_id, 'version': version,
                    'method': method, 'params': params,
                    'timeoutMs': max(1, int(self._remaining() * 1000))}
        if target is not None:
            envelope['targetClientId'] = target
        submitted = False
        try:
            # Encoding errors occur before any mutation is sent.
            json.dumps(envelope, allow_nan=False).encode('utf-8')
            submitted = True
            self._send(envelope)
            while True:
                response = self._read()
                kind = response.get('type')
                if kind == 'client-discovery-request':
                    self._send({'type': 'client-discovery-response',
                                'requestId': response.get('requestId'),
                                'response': {'canHandle': False}})
                    continue
                if kind != 'response' or response.get('requestId') != request_id:
                    continue
                if response.get('resultType') == 'error':
                    error = response.get('error')
                    # These router errors happen before a handler runs.  Other
                    # errors may follow a partially applied Desktop operation.
                    rejected = error in {'no-client-found', 'no-handler-for-request',
                                         'request-version-mismatch'}
                    code = 'not_loaded' if error == 'no-client-found' else 'rejected'
                    raise ControlError(code, uncertain=mutation and not rejected)
                if response.get('resultType') != 'success' or response.get('method') != method:
                    raise ControlError('protocol')
                if target is not None and response.get('handledByClientId') != target:
                    raise ControlError('protocol')
                if not isinstance(response.get('result'), dict):
                    raise ControlError('protocol')
                return response
        except ControlError as error:
            if error.code not in {'not_loaded', 'rejected', 'invalid_request'} and mutation and submitted:
                error.uncertain = True
            raise
        except socket.timeout:
            raise ControlError('timeout', uncertain=mutation and submitted) from None
        except (OSError, ValueError, UnicodeError):
            raise ControlError('disconnected', uncertain=mutation and submitted) from None


class _Exchange:
    def __init__(self, process, selector, deadline):
        self.process = process
        self.selector = selector
        self.deadline = deadline
        self.buffer = bytearray()
        self.read_bytes = 0

    def _remaining(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise ControlError('timeout')
        return remaining

    def write(self, data):
        stream = self.process.stdin
        self.selector.register(stream, selectors.EVENT_WRITE)
        try:
            offset = 0
            while offset < len(data):
                events = self.selector.select(self._remaining())
                if not events:
                    raise ControlError('timeout')
                for key, _ in events:
                    if key.fileobj is stream:
                        try:
                            written = os.write(stream.fileno(), data[offset:])
                        except BlockingIOError:
                            continue
                        if written <= 0:
                            raise ControlError('disconnected')
                        offset += written
                    else:
                        self._read()
        finally:
            self.selector.unregister(stream)

    def _read(self):
        try:
            chunk = os.read(self.process.stdout.fileno(), 65536)
        except BlockingIOError:
            return
        if not chunk:
            raise ControlError('disconnected')
        self.read_bytes += len(chunk)
        if self.read_bytes > MAX_OUTPUT_BYTES:
            raise ControlError('protocol')
        self.buffer.extend(chunk)

    def response(self, request_id):
        while True:
            self._remaining()
            while b'\n' in self.buffer:
                raw, _, rest = self.buffer.partition(b'\n')
                self.buffer = bytearray(rest)
                if not raw.strip():
                    continue
                try:
                    message = json.loads(raw)
                except (ValueError, UnicodeError):
                    raise ControlError('protocol') from None
                if not isinstance(message, dict):
                    raise ControlError('protocol')
                if 'method' in message:
                    # Never acknowledge approvals or user questions on behalf
                    # of the owner.  This control connection accepts no server
                    # requests; the Desktop's existing client owns them.
                    if 'id' in message:
                        self.write(ProxyClient._encode({'id': message['id'], 'error': {
                            'code': -32601, 'message': 'Unsupported client request',
                        }}))
                    continue
                if message.get('id') != request_id:
                    continue
                if 'error' in message:
                    detail = message['error']
                    code = detail.get('code') if isinstance(detail, dict) else None
                    raise ControlError('rejected', rpc_code=code if isinstance(code, int) else None)
                if 'result' not in message:
                    raise ControlError('protocol')
                return message['result']
            events = self.selector.select(self._remaining())
            if not events:
                raise ControlError('timeout')
            self._read()
