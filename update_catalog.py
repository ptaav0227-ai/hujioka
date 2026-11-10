#!/usr/bin/env python3
"""Respectful, incremental public-catalog monitor for MONST MASTER.

Schedule: GitHub Actions every 3 hours. Newest listing page always revisited;
older pages scanned round-robin. Listing attribute evidence may backfill only
EXISTING entries. Newly introduced entries require verified acquisition from
character profile. No credentials, game account access or protected APIs.
"""
from __future__ import annotations
import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
FEED = ROOT / 'catalog/latest.json'
STATE = ROOT / 'catalog/monitor_state.json'
BASE = 'https://game.technophere.com'
LIST = BASE + '/monst/characters/'
AGENT = 'MONST-MASTER-Catalog-Monitor/1.1 (public-pages; personal noncommercial use)'
EL = set('火水木光闇')
URL_RE = re.compile(r'/monst/char/(\d+)/?')
NO_RE = re.compile(r'No\.\s*(\d{1,6})')
PROFILE_NAME_RE = re.compile(r'^(?:【|〖)モンスト(?:】|〗)(.+?)(?:（|\(|の性能|の評価)')
FORMATION = re.compile(r'\s+(?:真獣神化|獣神化・改|獣神化改|獣神化|神化|進化|進化前|獣神化前|未進化)\s+')
SESSION = requests.Session()
SESSION.headers.update({'User-Agent': AGENT, 'Accept-Language':'ja,en;q=0.5'})


def get_json(p, default):
    try:
        data = json.loads(p.read_text('utf-8'))
        return data if isinstance(data, dict) else default
    except (OSError, ValueError):
        return default


def atomic_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temp.replace(path)


def get_soup(url):
    response = SESSION.get(url, timeout=18)
    response.raise_for_status()
    if len(response.content) > 3_000_000:
        raise ValueError('HTML response too large')
    return BeautifulSoup(response.text, 'html.parser')


def lines_of(soup):
    return [x.strip() for x in soup.get_text('\n', strip=True).splitlines() if x.strip()]


def parse_listing(soup, base_url):
    """Extract No., element, name and profile URL from catalog cards, not guesses."""
    found = {}
    for a in soup.select('a[href*="/monst/char/"]'):
        url = urljoin(base_url, a.get('href', ''))
        if urlparse(url).hostname != urlparse(BASE).hostname or not URL_RE.search(url):
            continue
        content = ' '.join(a.stripped_strings)
        no_match = NO_RE.search(content)
        if not no_match:
            continue
        no = int(no_match.group(1))
        prefix = content[:no_match.start()].strip()
        formation = FORMATION.search(prefix)
        if not formation:
            continue
        before = prefix[:formation.start()].strip()
        # The first single glyph is the catalog's fallback icon initial.
        if re.match(r'^\S\s+\S', before):
            before = before.split(' ', 1)[1].strip()
        element_region = prefix[formation.end():].strip()
        element = element_region[:1] if element_region[:1] in EL else ''
        if not before or len(before)>100 or not element:
            continue
        found[no] = {'dexNo': no, 'name': before, 'element': element,
                     'element_source': 'listing-verified-attribute', 'source_url': url}
    return found


def parse_profile(soup, page_url):
    lines = lines_of(soup)
    title = soup.find('h1')
    title_text = title.get_text(' ', strip=True) if title else ''
    name_match = PROFILE_NAME_RE.search(title_text)
    if not name_match:
        return None
    name = name_match.group(1).strip()
    if not name or len(name) > 100:
        return None
    head = ' '.join(lines[:90])
    head_match = re.search(r'([火水木光闇])\s*(?:真獣神化|獣神化・改|獣神化|進化|神化).*?No\.\s*(\d{1,6})',head)
    if not head_match:
        return None
    element, no = head_match.group(1), int(head_match.group(2))
    def field(label):
        for i, line in enumerate(lines[:160]):
            if line == label and i+1 < len(lines):
                return lines[i+1].strip()
        return ''
    acq = field('入手方法')
    if acq.startswith('ガチャ'):
        cat = '限定キャラ' if any(k in acq for k in ('限定','超獣','激獣','彩獣')) else '恒常キャラ'
    elif any(k in acq for k in ('ドロップ','クエスト','降臨')):
        cat = '降臨キャラ'
    elif any(k in acq for k in ('コラボ','配布')):
        cat = 'コラボ' if 'コラボ' in acq else '期間限定イベント'
    else:
        # An element is still useful for existing catalog records, even if
        # acquisition is not known. The browser will NOT add it as new.
        cat = ''
    icon = ''
    for img in soup.select('img'):
        alt = img.get('alt', '')
        if 'アイコン' not in alt and name not in alt:
            continue
        source = img.get('data-src') or img.get('src') or ''
        url = urljoin(page_url, source)
        if urlparse(url).scheme=='https' and urlparse(url).hostname:
            icon = url
            break
    return {'dexNo': no, 'name': name, 'element': element,
            'element_source': 'site-verified-metadata', 'source_url': page_url,
            **({'category':cat, 'acquisition':acq[:100]} if cat else {}),
            **({'icon':icon} if icon else {})}


def merge_entry(previous, candidate):
    result = dict(previous)
    if not result:
        result = dict(candidate)
    else:
        for field in ('name','element','category','acquisition','icon','source_url','element_source'):
            if candidate.get(field) and (not result.get(field) or
                (field=='element' and result.get(field) not in EL) or
                (field=='element_source' and candidate[field]=='site-verified-metadata')):
                result[field] = candidate[field]
    return result


def run(args):
    existing = get_json(FEED, {'characters': [], 'ex_categories': {}})
    cursor_info = get_json(STATE, {'next_page':2})
    merged = {int(c['dexNo']):c for c in existing.get('characters',[])
              if isinstance(c,dict) and str(c.get('dexNo','')).isdigit()}
    rp = RobotFileParser(); rp.set_url(BASE+'/robots.txt')
    try:
        rp.read()
        if not rp.can_fetch(AGENT, LIST):
            raise RuntimeError('robots.txt disallows catalog scan')
    except Exception as exc:
        print('Monitoring source unavailable, previous data kept:', exc, file=sys.stderr)
        return 0
    now = datetime.now(timezone.utc).isoformat()
    total_pages = max(1, int(cursor_info.get('total_pages', 50)))
    cursor = max(2,int(cursor_info.get('next_page',2)))
    pages = [1] + [((cursor-2+i) % max(1,total_pages-1))+2 for i in range(args.pages-1)]
    latest = set()
    pages_ok=0; attrs=0; prof=0; new=0; warnings=[]
    profile_candidates=[]
    for page in pages:
        url = LIST if page == 1 else LIST+f'page/{page}/'
        if not rp.can_fetch(AGENT,url):
            warnings.append('disallowed page '+str(page));continue
        try:
            soup = get_soup(url)
            text = soup.get_text(' ', strip=True)
            m = re.search(r'(\d+)\s*/\s*(\d+)\s*ページ',text)
            if m and int(m.group(2))>=1: total_pages=min(500,int(m.group(2)))
            listings = parse_listing(soup,url)
            if not listings: raise ValueError('no recognizable catalog cards')
            pages_ok+=1
            for no,item in listings.items():
                if page == 1: latest.add(no)
                old=merged.get(no)
                if old:
                    if old.get('element') not in EL:
                        merged[no]=merge_entry(old,item);attrs+=1
                    # Same No. but acquisition/icon still incomplete: verify profile.
                    if not old.get('acquisition') or not old.get('icon') or old.get('category') in ('','新キャラ'):
                        profile_candidates.append((no,item['source_url'],0))
                else:
                    # Publish listing-only evidence for existing in-browser records.
                    # The browser accepts it for backfilling, but never inserts
                    # a brand-new character until acquisition is verified.
                    merged[no]=item
                    profile_candidates.append((no,item['source_url'],1))
        except Exception as exc:
            warnings.append(f'page {page}: {str(exc)[:100]}')
        time.sleep(args.delay)
    # The next run continues the historical sweep, never skips page 1.
    next_page = ((cursor-2+max(0,args.pages-1)) % max(1,total_pages-1))+2
    # New arrivals are processed before old incomplete metadata. Both are
    # limited in quantity; incomplete characters remain eligible next scan.
    selected=[];seen=set()
    for no,url,is_new in sorted(profile_candidates,key=lambda z:(-z[2],-z[0])):
        if no not in seen:
            seen.add(no);selected.append((no,url,is_new))
    for no,url,is_new in selected[:args.profiles]:
        if not rp.can_fetch(AGENT,url): continue
        try:
            info=parse_profile(get_soup(url),url)
            if info and info['dexNo'] == no:
                if no not in merged and not info.get('acquisition'):
                    continue
                merged[no]=merge_entry(merged.get(no,{}),info)
                prof+=1
                if is_new:new+=1
        except Exception as exc:
            warnings.append(f'profile {no}: {str(exc)[:90]}')
        time.sleep(args.delay)
    if not pages_ok:
        print('No scanned pages. No data changes.',warnings,file=sys.stderr);return 0
    out={'schema':3,'generated_at':now,'source':LIST,
         'monitor':{'last_success':now,'pages_scanned':pages,'pages_total':total_pages,
                    'new_characters_verified':new,'attributes_discovered':attrs,
                    'profiles_verified':prof,'warnings':warnings[:8],
                    'next_page':next_page,'interval_note':'GitHub Actions schedule every 3 hours; may be delayed'},
         'characters': sorted(merged.values(),key=lambda c:c['dexNo'],reverse=True),
         'ex_categories':existing.get('ex_categories',{})}
    atomic_json(FEED,out)
    atomic_json(STATE,{'next_page':next_page,'total_pages':total_pages,'last_scan':now})
    print(f'pages {pages}; verified profiles {prof}; new {new}; missing attributes {attrs}; feed {len(merged)}; next page {next_page}')
    for w in warnings: print('WARNING',w,file=sys.stderr)
    return 0


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--pages',type=int,default=5)
    p.add_argument('--profiles',type=int,default=42)
    p.add_argument('--delay',type=float,default=1.0)
    args=p.parse_args()
    if args.pages<2 or args.pages>12 or args.profiles<1 or args.profiles>100 or args.delay<0.5:
        p.error('invalid run limits')
    return run(args)

if __name__ == '__main__':raise SystemExit(main())
