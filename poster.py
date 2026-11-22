#!/usr/bin/env python3
"""Foldville Instagram poster.

Posts the reels and carousels in schedule.json at their times through Instagram's API
(Instagram API with Instagram Login). It runs on GitHub Actions every 15 minutes
(.github/workflows/post.yml) and uses only the Python standard library.

Modes (MODE, or request.json on a push):
  post    (default) publish whatever is due, then record it on the `state` branch
  dryrun  prepare the next items (or ITEM ids) on Instagram WITHOUT publishing them
  check   check the key, the posting limit and that the next files are online

The key comes from the repository secret IG_TOKEN and is never printed.
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.environ.get('STATE_DIR', os.path.join(ROOT, 'state'))
STATE_FILE = os.path.join(STATE_DIR, 'state.json')
TOKEN = os.environ.get('IG_TOKEN', '').strip()
REPO = os.environ.get('GITHUB_REPOSITORY', '')
OWNER = os.environ.get('GITHUB_REPOSITORY_OWNER', '')
GH_TOKEN = os.environ.get('GITHUB_TOKEN', '')
RUN_URL = (f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/{REPO}/actions/runs/"
           f"{os.environ.get('GITHUB_RUN_ID', '')}")


def load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return default


CFG = load(os.path.join(ROOT, 'config.json'), {})
# Unified login tokens work on graph.facebook.com; Instagram-login tokens on graph.instagram.com
API = CFG.get('api_host', 'https://graph.facebook.com').rstrip('/')
LEAD = timedelta(minutes=CFG.get('prepare_minutes_before', 10))
LOOK = timedelta(minutes=CFG.get('lookahead_minutes', 26))
LATE = timedelta(hours=CFG.get('late_hours', 3))
MAX_TRIES = CFG.get('max_tries', 3)


def now():
    return datetime.now(timezone.utc)


def log(*a):
    print(now().strftime('%H:%M:%S'), *a, flush=True)


def when(item):
    return datetime.fromisoformat(item['when'])


def local(dt):
    """Schedule times carry the Polish offset already, so they print as Polish time."""
    return dt.strftime('%a %d %b %H:%M')


class ApiError(Exception):
    def __init__(self, msg, code=None):
        super().__init__(msg)
        self.code = code


# ---------- Instagram API ----------

def api(method, path, **params):
    if not TOKEN:
        raise ApiError('IG_TOKEN secret is missing', 190)
    params['access_token'] = TOKEN
    body = urllib.parse.urlencode(params)
    url = f'{API}/{path}'
    for attempt in range(4):
        if method == 'GET':
            req = urllib.request.Request(f'{url}?{body}')
        else:
            req = urllib.request.Request(url, data=body.encode(), method=method)
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            raw = e.read().decode('utf-8', 'replace')
            try:
                err = json.loads(raw).get('error', {})
            except ValueError:
                err = {'message': raw[:300]}
            code = err.get('code')
            msg = (f"HTTP {e.code}, code {code}/{err.get('error_subcode')}: {err.get('message')} "
                   f"{err.get('error_user_title', '')} {err.get('error_user_msg', '')}").strip()
            transient = e.code >= 500 or err.get('is_transient') or code in (1, 2, 4, 17, 32, 341, 613)
            if transient and attempt < 3:
                log('Instagram busy, retrying:', msg)
                time.sleep(30 * (attempt + 1))
                continue
            raise ApiError(msg, code)
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            if attempt < 3:
                log('network problem, retrying:', e)
                time.sleep(30 * (attempt + 1))
                continue
            raise ApiError(f'network problem: {e}')


def whoami():
    """Instagram account to post to: config ig_user_id, else the token's own account (Instagram login)."""
    ig = str(CFG.get('ig_user_id') or '')
    if ig:
        try:
            me = api('GET', ig, fields='id,username,followers_count,media_count')
        except ApiError as e:
            if e.code == 190:
                raise
            me = api('GET', ig, fields='id,username')
        return ig, me
    me = api('GET', 'me', fields='user_id,username,account_type')
    return str(me.get('user_id') or me['id']), me


def online(url):
    try:
        req = urllib.request.Request(url, method='HEAD', headers={'User-Agent': 'foldville-poster'})
        with urllib.request.urlopen(req, timeout=45) as r:
            return r.status == 200 and int(r.headers.get('Content-Length') or 1) > 0
    except Exception as e:  # noqa: BLE001
        log('not online yet:', url, '-', e)
        return False


def link(base, rel):
    return base.rstrip('/') + '/' + urllib.parse.quote(rel)


def bases_for(files):
    """Media bases (GitHub Pages first, raw files second) on which all these files are online."""
    ok = [b for b in CFG.get('media_bases', []) if all(online(link(b, f)) for f in files)]
    if not ok:
        raise ApiError('the files are not online: ' + ', '.join(files))
    return ok


def wait_ready(cid, limit=900):
    t0 = time.time()
    while True:
        st = api('GET', cid, fields='status_code,status')
        code = st.get('status_code')
        if code == 'FINISHED':
            return
        if code in ('ERROR', 'EXPIRED'):
            raise ApiError(f'Instagram could not prepare it ({code}): {st.get("status")}')
        if time.time() - t0 > limit:
            raise ApiError(f'still {code} after {limit // 60} minutes')
        time.sleep(15)


def caption(item):
    text = item['caption']
    line = CFG.get('december_sale_line', '')
    if item.get('dec_line') and line and '\n\nComment ' in text:
        text = text.replace('\n\nComment ', f'\n\n{line}\n\nComment ', 1)
    return text


def prepare(ig, item):
    """Creates the post on Instagram without publishing it; returns (container id, media base used)."""
    files = [item['video'], item['cover']] if item['kind'] == 'reel' else item['images']
    last = None
    for base in bases_for(files):
        try:
            if item['kind'] == 'reel':
                p = dict(media_type='REELS', caption=caption(item), video_url=link(base, item['video']),
                         cover_url=link(base, item['cover']))
                if item.get('trial'):
                    p['trial_params'] = json.dumps({'graduation_strategy': CFG.get('trial_graduation', 'SS_PERFORMANCE')})
                else:
                    p['share_to_feed'] = 'true'
            else:
                kids = [api('POST', f'{ig}/media', image_url=link(base, f), is_carousel_item='true')['id']
                        for f in item['images']]
                for k in kids:
                    wait_ready(k, 300)
                p = dict(media_type='CAROUSEL', caption=caption(item), children=','.join(kids))
            if CFG.get('ai_label', True):
                p['is_ai_generated'] = 'true'
            cid = api('POST', f'{ig}/media', **p)['id']
            wait_ready(cid)
            return cid, base
        except ApiError as e:
            if e.code == 190:
                raise
            last = e
            log('failed with', base, '-', e)
    raise last


def publish(ig, cid):
    mid = api('POST', f'{ig}/media_publish', creation_id=cid)['id']
    info = {}
    for fields in ('permalink,timestamp,is_ai_generated', 'permalink,timestamp'):
        try:
            info = api('GET', mid, fields=fields)
            break
        except ApiError as e:
            log('could not read the new post back:', e)
    return mid, info


# ---------- state, issues, reports ----------

def git(*args, check=True):
    return subprocess.run(['git', '-C', STATE_DIR, *args], check=check, capture_output=True, text=True)


def save_state(st, message):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(STATE_FILE, 'w') as f:
        json.dump(st, f, indent=1, ensure_ascii=False)
        f.write('\n')
    if not os.path.isdir(os.path.join(STATE_DIR, '.git')):
        log('(no state branch checked out, state kept locally)')
        return
    git('add', '-A')
    if git('diff', '--cached', '--quiet', check=False).returncode == 0:
        return
    git('commit', '-m', message)
    for attempt in range(6):
        if git('push', 'origin', 'HEAD:state', check=False).returncode == 0:
            return
        git('pull', '--rebase', 'origin', 'state', check=False)
        time.sleep(10 * (attempt + 1))
    raise RuntimeError('could not save the state branch')


def issue(st, key, title, body):
    """Opens a GitHub issue once per key, so the owner gets an email."""
    if key in st.setdefault('issues', {}) or not (REPO and GH_TOKEN):
        return
    data = json.dumps({'title': title, 'body': f'@{OWNER} {body}\n\nRun: {RUN_URL}'}).encode()
    req = urllib.request.Request(f'https://api.github.com/repos/{REPO}/issues', data=data, method='POST',
                                 headers={'Authorization': f'Bearer {GH_TOKEN}',
                                          'Accept': 'application/vnd.github+json'})
    try:
        with urllib.request.urlopen(req, timeout=30):
            st['issues'][key] = now().isoformat(timespec='seconds')
    except Exception as e:  # noqa: BLE001
        log('could not open an issue:', e)


def write_report(name, lines):
    path = os.path.join(STATE_DIR, 'status', name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as f:
        f.write('\n'.join(lines) + '\n')


KEY_HELP = ('Make a new key: developers.facebook.com > My Apps > Foldville Poster > Use cases > Customize > '
            'API setup with Instagram login > Generate token > Copy. Then in this repository: Settings > '
            'Secrets and variables > Actions > IG_TOKEN > Update, paste it and save.')


def key_reminder(st):
    exp = CFG.get('token_expires')
    if exp and date.today() >= date.fromisoformat(exp) - timedelta(days=7):
        issue(st, f'key-{exp}', f'Instagram key expires on {exp}', KEY_HELP)


# ---------- modes ----------

def sleep_until(t):
    secs = (t - now()).total_seconds()
    if secs > 0:
        log(f'waiting {int(secs // 60)} min {int(secs % 60)} s')
        time.sleep(secs)


def run_post(st):
    if CFG.get('paused'):
        log('paused in config.json, nothing posted')
        return 0
    sched = load(os.path.join(ROOT, 'schedule.json'), {'items': []})['items']
    done, missed, failed = st.setdefault('done', {}), st.setdefault('missed', {}), st.setdefault('failed', {})
    tries = st.setdefault('tries', {})
    todo = sorted((i for i in sched if i['id'] not in done and i['id'] not in missed and i['id'] not in failed),
                  key=when)
    ig = None
    errors = 0
    for item in todo:
        t, due = now(), when(item)
        if t > due + LATE:
            missed[item['id']] = {'when': item['when'], 'noticed': t.isoformat(timespec='seconds')}
            issue(st, f"missed-{item['id']}", f"Not posted: {item['id']} ({local(due)})",
                  'This post was not published on time (GitHub did not start the poster). '
                  'Post it from the phone guide if it still makes sense, or skip it.')
            save_state(st, f"missed {item['id']}")
            continue
        if due - t > LOOK:
            break
        try:
            if ig is None:
                ig, me = whoami()
                log('posting as', me.get('username'))
            sleep_until(due - LEAD)
            log('preparing', item['id'], 'for', local(due))
            cid, base = prepare(ig, item)
            sleep_until(due)
            mid, info = publish(ig, cid)
            done[item['id']] = {'when': item['when'], 'posted': now().isoformat(timespec='seconds'), 'media_id': mid,
                                'permalink': info.get('permalink'), 'ai_label': info.get('is_ai_generated'),
                                'from': base}
            log('POSTED', item['id'], info.get('permalink', mid))
            save_state(st, f"posted {item['id']}")
        except ApiError as e:
            errors += 1
            log('FAILED', item['id'], '-', e)
            if e.code == 190:
                issue(st, 'key-invalid', 'Instagram key stopped working', f'Instagram says: {e}\n\n{KEY_HELP}')
                save_state(st, 'key problem')
                break
            tries[item['id']] = tries.get(item['id'], 0) + 1
            if tries[item['id']] >= MAX_TRIES:
                failed[item['id']] = {'when': item['when'], 'error': str(e)[:500]}
                issue(st, f"failed-{item['id']}", f"Could not post {item['id']} ({local(due)})",
                      f'Instagram says: {e}\n\nPost it from the phone guide, or ask Claude to look at it.')
            save_state(st, f"error {item['id']}")
            break
    return errors


def who(me):
    """One line about the account for the reports."""
    out = f"@{me.get('username')}"
    if me.get('followers_count') is not None:
        out += f", {me['followers_count']} followers, {me.get('media_count')} posts"
    return out


def pick_items(ids, n=2):
    sched = load(os.path.join(ROOT, 'schedule.json'), {'items': []})['items']
    if ids:
        want = [x.strip() for x in ids.split(',') if x.strip()]
        return [i for i in sched if i['id'] in want]
    done = load(STATE_FILE, {}).get('done', {})
    return sorted((i for i in sched if i['id'] not in done and when(i) > now()), key=when)[:n]


def run_dryrun(ids):
    ig, me = whoami()
    lines = [f'# Test run {now():%Y-%m-%d %H:%M} UTC (nothing published)', '',
             f"Account: {who(me)}", '']
    ok = True
    for item in pick_items(ids):
        try:
            cid, base = prepare(ig, item)
            lines.append(f"- {item['id']} ({local(when(item))}): READY on Instagram, not published (files from {base})")
        except ApiError as e:
            ok = False
            lines.append(f"- {item['id']} ({local(when(item))}): FAILED: {e}")
    write_report('test-run.md', lines)
    log('\n'.join(lines))
    return 0 if ok else 1


def discover():
    """Lists the Instagram accounts this key can reach (to fill ig_user_id in config.json)."""
    found = []
    tries = [('me', dict(fields='id,name,user_id,username')),
             ('me/accounts', dict(fields='name,instagram_business_account{id,username}')),
             ('me/businesses', dict(fields='name,owned_instagram_accounts{id,username},instagram_accounts{id,username}')),
             ('me/instagram_accounts', dict(fields='id,username'))]
    for path, params in tries:
        try:
            found.append(f'{path}: {json.dumps(api("GET", path, **params), ensure_ascii=False)[:800]}')
        except ApiError as e:
            found.append(f'{path}: not available ({str(e)[:160]})')
    return found


def run_check(ids):
    if not CFG.get('ig_user_id'):
        lines = [f'# Account lookup {now():%Y-%m-%d %H:%M} UTC', ''] + [f'- {x}' for x in discover()]
        write_report('check.md', lines)
        log('\n'.join(lines))
        return 0
    ig, me = whoami()
    lines = [f'# Check {now():%Y-%m-%d %H:%M} UTC', '', f"Account: {who(me)}"]
    try:
        lim = api('GET', f'{ig}/content_publishing_limit', fields='quota_usage,config')
        lines.append(f'Posting limit: {json.dumps(lim.get("data", lim))}')
    except ApiError as e:
        lines.append(f'Posting limit: could not read ({e})')
    ok = True
    for item in pick_items(ids, 5):
        files = [item['video'], item['cover']] if item['kind'] == 'reel' else item['images']
        bases = [b for b in CFG.get('media_bases', []) if all(online(link(b, f)) for f in files)]
        ok &= bool(bases)
        lines.append(f"- {item['id']}: files online on {len(bases)} of {len(CFG.get('media_bases', []))} links")
    write_report('check.md', lines)
    log('\n'.join(lines))
    return 0 if ok else 1


def main():
    mode, ids = os.environ.get('MODE', 'post') or 'post', os.environ.get('ITEM', '')
    if os.environ.get('GITHUB_EVENT_NAME') == 'push':
        req = load(os.path.join(ROOT, 'request.json'), {})
        mode, ids = req.get('mode', 'check'), req.get('item', '')
    st = load(STATE_FILE, {})
    log('mode', mode, ids)
    try:
        if mode == 'post':
            code = run_post(st)
        elif mode == 'dryrun':
            code = run_dryrun(ids)
        else:
            code = run_check(ids)
    except ApiError as e:
        log('ERROR', e)
        if e.code == 190 and mode == 'post':
            issue(st, 'key-invalid', 'Instagram key stopped working', f'Instagram says: {e}\n\n{KEY_HELP}')
        code = 1
    key_reminder(st)
    save_state(st, f'{mode} run')
    sys.exit(code)


if __name__ == '__main__':
    main()
