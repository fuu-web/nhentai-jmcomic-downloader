import re
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlparse


CHALLENGE_MARKERS = (
    'just a moment', 'checking your browser', 'verify you are human',
    'attention required', 'cf-chl-', 'cf-turnstile',
    'please wait', 'rate limit', 'temporarily blocked',
    '请稍候', '请等待', '验证您是真人', '安全检查', '访问过于频繁',
)


class ChallengeDetected(RuntimeError):
    pass


def detect_challenge_response(response):
    """Detect anti-bot waiting/challenge pages, including HTTP 200 pages."""
    status = getattr(response, 'status_code', None)
    headers = getattr(response, 'headers', {}) or {}
    content_type = str(headers.get('Content-Type', '')).lower()
    if status in (403, 429, 503):
        return f'HTTP {status}'
    if 'text/' not in content_type and 'html' not in content_type:
        return None
    try:
        text = str(getattr(response, 'text', '') or '').lower()
    except Exception:
        return None
    for marker in CHALLENGE_MARKERS:
        if marker in text[:20000]:
            return marker
    return None


def parse_proxy_pool(value):
    """Parse a comma/semicolon/newline separated proxy list."""
    if isinstance(value, (list, tuple)):
        values = value
    else:
        values = re.split(r'[,;\n]+', str(value or ''))
    proxies = []
    for item in values:
        if item is None:
            proxy = None
            if proxy not in proxies:
                proxies.append(proxy)
            continue
        proxy = str(item).strip()
        if proxy.lower() in {'direct', 'direct://', '直连'}:
            proxy = None
        if proxy not in proxies:
            proxies.append(proxy)
    return proxies or [None]


def endpoint_from_url(url):
    return urlparse(str(url)).hostname or str(url)


@dataclass
class RouteState:
    successes: int = 0
    failures: int = 0
    consecutive_failures: int = 0
    ewma_latency: float = 0.0
    blocked_until: float = 0.0
    last_error: str = ''


class RequestLease:
    def __init__(self, scheduler, endpoint, proxy, key):
        self.scheduler = scheduler
        self.endpoint = endpoint
        self.proxy = proxy
        self.key = key
        self.started_at = time.monotonic()
        self._finished = False

    def finish(self, status=None, error=None, retry_after=None):
        if self._finished:
            return
        self._finished = True
        self.scheduler._finish(self, status, error, retry_after)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, _traceback):
        self.finish(error=exc_value)


class AdaptiveScheduler:
    """Thread-safe adaptive concurrency, route scoring and circuit breaker."""

    def __init__(self, max_concurrency=8, min_concurrency=1,
                 failure_threshold=3, cooldown=20, recovery_successes=8,
                 proxies=None, clock=None, initial_concurrency=3):
        self.max_concurrency = max(1, int(max_concurrency))
        self.min_concurrency = max(1, min(int(min_concurrency), self.max_concurrency))
        self.current_limit = min(
            self.max_concurrency, max(self.min_concurrency, int(initial_concurrency)))
        self._normal_initial_limit = self.current_limit
        self.failure_threshold = max(1, int(failure_threshold))
        self.base_cooldown = max(1.0, float(cooldown))
        self.recovery_successes = max(2, int(recovery_successes))
        self.proxies = parse_proxy_pool(proxies)
        self._clock = clock or time.monotonic
        self._condition = threading.Condition(threading.RLock())
        self._states = {}
        self._active = 0
        self._stable_successes = 0
        self._request_count = 0
        self._blocked_count = 0
        self._site_blocked_until = 0.0
        self._site_block_reason = ''
        self._next_request_at = 0.0
        self._browser_priority = False
        self._request_interval = 0.05

    def configure(self, max_concurrency=None, proxies=None, browser_priority=None,
                  request_interval=None):
        with self._condition:
            if max_concurrency is not None:
                self.max_concurrency = max(1, int(max_concurrency))
                self.current_limit = min(self.current_limit, self.max_concurrency)
                self._normal_initial_limit = min(
                    self.max_concurrency, max(self.min_concurrency, self._normal_initial_limit))
            if proxies is not None:
                self.proxies = parse_proxy_pool(proxies)
            if browser_priority is not None:
                self._browser_priority = bool(browser_priority)
                if self._browser_priority:
                    self.current_limit = min(self.current_limit, 2)
                else:
                    self.current_limit = max(
                        self.current_limit, self._normal_initial_limit)
            if request_interval is not None:
                self._request_interval = max(0.0, float(request_interval))
            self._condition.notify_all()

    def pause_site(self, seconds, reason='检测到等待或验证页面'):
        with self._condition:
            self._site_blocked_until = max(
                self._site_blocked_until, self._clock() + max(1.0, float(seconds)))
            self._site_block_reason = str(reason)
            self.current_limit = self.min_concurrency
            self._stable_successes = 0
            self._blocked_count += 1
            self._condition.notify_all()

    @staticmethod
    def _key(endpoint, proxy):
        return str(endpoint or 'default'), proxy or 'DIRECT'

    def _state(self, key):
        return self._states.setdefault(key, RouteState())

    def _score(self, key, now):
        state = self._state(key)
        blocked = state.blocked_until > now
        total = state.successes + state.failures
        failure_rate = state.failures / total if total else 0.0
        latency = state.ewma_latency or 1.0
        return (1 if blocked else 0, state.consecutive_failures,
                failure_rate, latency, state.failures - state.successes)

    def acquire(self, endpoints, proxies=None, cancel_event=None):
        endpoints = list(dict.fromkeys(str(item) for item in endpoints if item)) or ['default']
        proxy_pool = parse_proxy_pool(self.proxies if proxies is None else proxies)
        with self._condition:
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    raise InterruptedError('用户取消')
                now = self._clock()
                if self._site_blocked_until > now:
                    self._condition.wait(max(
                        0.05, min(1.0, self._site_blocked_until - now)))
                    continue
                if self._next_request_at > now:
                    self._condition.wait(max(
                        0.01, min(0.5, self._next_request_at - now)))
                    continue
                routes = [(endpoint, proxy, self._key(endpoint, proxy))
                          for endpoint in endpoints for proxy in proxy_pool]
                available = [route for route in routes
                             if self._state(route[2]).blocked_until <= now]
                if available and self._active < self.current_limit:
                    endpoint, proxy, key = min(available,
                                               key=lambda route: self._score(route[2], now))
                    self._active += 1
                    self._request_count += 1
                    interval = 0.75 if self._browser_priority else self._request_interval
                    self._next_request_at = now + interval
                    return RequestLease(self, endpoint, proxy, key)

                if not available:
                    earliest = min(self._state(route[2]).blocked_until for route in routes)
                    wait_time = max(0.05, min(1.0, earliest - now))
                else:
                    wait_time = 0.2
                self._condition.wait(wait_time)

    def _finish(self, lease, status, error, retry_after):
        latency = max(0.001, self._clock() - lease.started_at)
        with self._condition:
            state = self._state(lease.key)
            self._active = max(0, self._active - 1)
            state.ewma_latency = latency if state.ewma_latency == 0 else \
                state.ewma_latency * 0.75 + latency * 0.25

            error_text = str(error or '')
            lower = error_text.lower()
            challenge = isinstance(error, ChallengeDetected)
            risk = challenge or status in (403, 429) or any(
                word in lower for word in ('cloudflare', 'captcha', 'turnstile', 'restricted access'))
            transient = error is not None or status in (408, 425, 500, 502, 503, 504, 520, 521, 522, 523, 524)
            neutral = status == 404

            if not error and status is not None and 200 <= int(status) < 400:
                state.successes += 1
                state.consecutive_failures = 0
                state.last_error = ''
                state.blocked_until = 0.0
                self._stable_successes += 1
                if self._stable_successes >= self.recovery_successes and \
                        self.current_limit < self.max_concurrency:
                    self.current_limit += 1
                    self._stable_successes = 0
            elif neutral:
                state.consecutive_failures = 0
            elif risk or transient:
                state.failures += 1
                state.consecutive_failures += 1
                state.last_error = error_text or f'HTTP {status}'
                self._stable_successes = 0
                if risk:
                    self.current_limit = max(self.min_concurrency, self.current_limit // 2)
                else:
                    self.current_limit = max(self.min_concurrency, self.current_limit - 1)
                if risk or state.consecutive_failures >= self.failure_threshold:
                    multiplier = min(8, 2 ** max(0, state.consecutive_failures - self.failure_threshold))
                    cooldown = float(retry_after or self.base_cooldown) * multiplier
                    state.blocked_until = self._clock() + min(300.0, cooldown)
                    self._blocked_count += 1
                if challenge:
                    self._site_blocked_until = max(
                        self._site_blocked_until, self._clock() + 45.0)
                    self._site_block_reason = error_text
            self._condition.notify_all()

    def snapshot(self):
        with self._condition:
            now = self._clock()
            routes = []
            for (endpoint, proxy), state in self._states.items():
                routes.append({
                    'endpoint': endpoint,
                    'proxy': None if proxy == 'DIRECT' else proxy,
                    'successes': state.successes,
                    'failures': state.failures,
                    'latency': state.ewma_latency,
                    'cooldown': max(0.0, state.blocked_until - now),
                    'last_error': state.last_error,
                })
            return {
                'limit': self.current_limit,
                'max_limit': self.max_concurrency,
                'active': self._active,
                'requests': self._request_count,
                'blocked_events': self._blocked_count,
                'open_routes': sum(1 for route in routes if route['cooldown'] > 0),
                'site_cooldown': max(0.0, self._site_blocked_until - now),
                'site_block_reason': self._site_block_reason if self._site_blocked_until > now else '',
                'browser_priority': self._browser_priority,
                'routes': routes,
            }


class JmAdaptiveStrategy:
    """Adapter for jmcomic's domain_retry_strategy callback."""

    def __init__(self, scheduler, stop_event=None):
        self.scheduler = scheduler
        self.stop_event = stop_event

    def __call__(self, client, request=None, url=None, is_image=False, **kwargs):
        from urllib.parse import urlparse

        if request is None:
            return self

        if url.startswith('/'):
            endpoints = list(client.domain_list)
        else:
            endpoints = [urlparse(url).hostname or url]
        attempts = max(1, (client.retry_times + 1) * len(endpoints))
        last_error = None
        for _attempt in range(attempts):
            lease = self.scheduler.acquire(endpoints, cancel_event=self.stop_event)
            request_url = client.of_api_url(url, lease.endpoint) if url.startswith('/') else url
            request_kwargs = dict(kwargs)
            client.update_request_with_specify_domain(request_kwargs,
                                                      lease.endpoint if url.startswith('/') else None,
                                                      is_image)
            if lease.proxy:
                request_kwargs['proxies'] = {
                    'http': lease.proxy,
                    'https': lease.proxy,
                }
            else:
                request_kwargs['proxies'] = None
            try:
                response = request(request_url, **request_kwargs)
                status = getattr(response, 'status_code', None)
                challenge = detect_challenge_response(response)
                if challenge:
                    lease.finish(status=status, error=ChallengeDetected(challenge))
                    if hasattr(response, 'close'):
                        response.close()
                    raise ChallengeDetected(challenge)
                if status in (403, 408, 425, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524):
                    retry_after = getattr(response, 'headers', {}).get('Retry-After')
                    lease.finish(status=status, retry_after=retry_after)
                    if hasattr(response, 'close'):
                        response.close()
                    raise RuntimeError(f'HTTP {status}')
                response = client.raise_if_resp_should_retry(response, is_image)
                lease.finish(status=status)
                return response
            except Exception as error:
                last_error = error
                status = getattr(getattr(error, 'resp', None), 'status_code', None)
                lease.finish(status=status, error=error)
                client.before_retry(error, request_kwargs, _attempt, request_url)
                if self.stop_event is not None and self.stop_event.is_set():
                    raise InterruptedError('用户取消')
        raise last_error or RuntimeError('所有线路均不可用')
