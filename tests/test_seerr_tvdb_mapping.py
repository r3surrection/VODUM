"""Seerr series requests without TVDB id, and reconciliation of lost answers (fork addition).

Seerr answers are mocked; the routing around the request (destinations, user
lookup, authorization) is replaced by small stubs so only the mapping and
reconciliation logic runs.
"""
import sqlite3

import pytest

from core import portal_media_request_jobs as jobs
from core import portal_media_request_tracking as tracking
from core import portal_media_requests as pmr
from core.portal_media_requests import MediaRequestError
from core.portal_media_requests_tvdb import normalize_candidates, resolve_tvdb

TMDB = 278144
SERVER = {'id': 5, 'type': 'seerr', 'settings_json': '{}'}
LIBRARY = {'id': 1, 'name': 'Serien', 'preferred': 1, 'request_priority': 100}


def candidate(tvdb, tmdb=None, title='Checkerin Marina', year=2024):
    return {'tvdbId': tvdb, 'tmdbId': tmdb, 'title': title, 'year': year,
            'images': [{'coverType': 'poster', 'remoteUrl': f'https://artworks.thetvdb.com/p/{tvdb}.jpg'}]}


class FakeSeerr:
    def __init__(self, *, tvdb=None, candidates=(), requests=(), post_error=None, status=None):
        self.details = {'id': TMDB, 'externalIds': {'tvdbId': tvdb}, 'genres': [],
                        'mediaInfo': {'status': status} if status else None,
                        'seasons': [{'seasonNumber': 1, 'episodeCount': 8}]}
        self.candidates = list(candidates)
        self.requests = list(requests)
        self.post_error = post_error
        self.posts = []
        self.paths = []

    def __call__(self, server, path, *, params=None, payload=None):
        self.paths.append(path)
        if payload is not None:
            self.posts.append(payload)
            if self.post_error:
                raise self.post_error
            return {'id': 77, 'status': 2}
        if path == f'/api/v1/tv/{TMDB}':
            return self.details
        if path == '/api/v1/movie/9':
            return {'id': 9, 'externalIds': {}, 'genres': []}
        if path == '/api/v1/service/sonarr':
            return [{'id': 1, 'name': 'Sonarr'}]
        if path == f'/api/v1/service/sonarr/lookup/{TMDB}':
            return self.candidates
        if path == '/api/v1/request':
            return {'results': self.requests}
        raise AssertionError('unexpected Seerr call ' + path)


@pytest.fixture
def seerr(monkeypatch):
    holder = {}

    def install(fake):
        holder['fake'] = fake
        monkeypatch.setattr(pmr, 'api', fake)
    monkeypatch.setattr(pmr, '_request_destinations', lambda *a, **k: ([(LIBRARY, [SERVER])], {1}))
    monkeypatch.setattr(pmr, '_eligible_destinations', lambda destinations, metadata, automatic: [(LIBRARY, None, [SERVER])])
    monkeypatch.setattr(pmr, '_authorized_before_post', lambda *a, **k: None)
    monkeypatch.setattr(pmr, 'request_options', lambda db: ('seerr', 10))
    monkeypatch.setattr(pmr, 'linked_arrs', lambda *a, **k: [SERVER])
    monkeypatch.setattr(pmr, '_member_identity_keys', lambda *a: {})
    monkeypatch.setattr(pmr, 'seerr_find_user', lambda *a: {'id': 7, 'permissions': 0})
    return install


def submit(kind='series', external_id=TMDB, **kwargs):
    seen = []
    result = pmr.submit_seerr_request(None, 1, None, kind, external_id, on_submitted=seen.append, **kwargs)
    return result, seen


def existing_request(**overrides):
    row = {'id': 55, 'status': 2, 'is4k': False, 'media': {'tmdbId': TMDB, 'mediaType': 'tv'},
           'requestedBy': {'id': 7}, 'seasons': [{'seasonNumber': 1}]}
    row.update(overrides)
    return row


def test_unique_tmdb_match_is_taken_over(seerr):
    fake = FakeSeerr(candidates=[candidate(1, 111), candidate(441454, TMDB), candidate(3, None)])
    seerr(fake)
    result, seen = submit()
    assert result == 'portal_requests_added'
    assert fake.posts[0]['tvdbId'] == 441454
    assert seen[-1]['seerr_request_id'] == 77


def test_known_tvdb_id_skips_the_lookup(seerr):
    fake = FakeSeerr(tvdb=999, candidates=[candidate(1, TMDB)])
    seerr(fake)
    submit()
    assert fake.posts[0]['tvdbId'] == 999
    assert not any('service/sonarr' in path for path in fake.paths)


def test_ambiguous_candidates_need_a_choice(seerr):
    fake = FakeSeerr(candidates=[candidate(1, TMDB), candidate(2, TMDB)])
    seerr(fake)
    with pytest.raises(MediaRequestError) as error:
        submit()
    assert str(error.value) == 'portal_requests_seerr_tvdb_unresolved'
    assert fake.posts == []


def test_members_choice_is_sent_and_must_be_offered(seerr):
    fake = FakeSeerr(candidates=[candidate(1, TMDB), candidate(2, TMDB)])
    seerr(fake)
    submit(tvdb_choice=2)
    assert fake.posts[0]['tvdbId'] == 2
    fake.posts.clear()
    with pytest.raises(MediaRequestError):
        submit(tvdb_choice=999)
    assert fake.posts == []


def test_no_candidates_gives_a_clear_error(seerr):
    fake = FakeSeerr(candidates=[])
    seerr(fake)
    with pytest.raises(MediaRequestError) as error:
        submit()
    assert str(error.value) == 'portal_requests_seerr_tvdb_unresolved'
    assert fake.posts == []


def test_existing_request_is_attached_instead_of_posted(seerr):
    fake = FakeSeerr(candidates=[candidate(441454, TMDB)], requests=[
        existing_request(requestedBy={'id': 8}, id=1), existing_request(id=55)])
    seerr(fake)
    result, seen = submit()
    assert fake.posts == []
    assert seen[-1]['seerr_request_id'] == 55
    assert result == 'portal_requests_added'


def test_request_of_another_user_or_declined_is_ignored(seerr):
    fake = FakeSeerr(candidates=[candidate(441454, TMDB)], requests=[
        existing_request(requestedBy={'id': 8}), existing_request(status=3, id=2)])
    seerr(fake)
    submit()
    assert len(fake.posts) == 1


def test_lost_answer_is_reconciled_with_the_existing_request(seerr):
    error = MediaRequestError('portal_requests_add_uncertain')
    fake = FakeSeerr(tvdb=441454, post_error=error)
    # The request only appears in Seerr once the POST went out.
    class Late(FakeSeerr):
        def __call__(self, server, path, *, params=None, payload=None):
            if payload is None and path == '/api/v1/request' and not self.posts:
                return {'results': []}
            if payload is None and path == '/api/v1/request':
                return {'results': [existing_request(id=91)]}
            return super().__call__(server, path, params=params, payload=payload)
    late = Late(tvdb=441454, post_error=error)
    seerr(late)
    result, seen = submit()
    assert seen[-1]['seerr_request_id'] == 91
    assert result == 'portal_requests_added'


def test_lost_answer_without_request_stays_uncertain(seerr):
    fake = FakeSeerr(tvdb=441454, post_error=MediaRequestError('portal_requests_add_uncertain'))
    seerr(fake)
    with pytest.raises(MediaRequestError) as error:
        submit()
    assert str(error.value) == 'portal_requests_add_uncertain'
    # The server is remembered before the POST, so the job can be reconciled later.
    assert pmr.api is fake


def test_movie_flow_is_untouched(seerr):
    fake = FakeSeerr()
    seerr(fake)
    result, seen = submit('movie', 9)
    assert result == 'portal_requests_added'
    assert fake.posts == [{'mediaType': 'movie', 'mediaId': 9, 'userId': 7, 'is4k': False}]
    assert fake.paths == ['/api/v1/movie/9', '/api/v1/request']
    assert seen[-1]['seerr_request_id'] == 77


def test_candidate_helpers():
    rows = normalize_candidates([candidate(1, TMDB), candidate(1, TMDB), {'tvdbId': 'x'}, candidate(2, None)])
    assert [row['tvdb_id'] for row in rows] == [1, 2]
    assert rows[0]['poster'].startswith('https://artworks.thetvdb.com/')
    assert resolve_tvdb(rows, TMDB) == ('auto', 1)
    assert resolve_tvdb(rows, 5)[0] == 'choose'
    assert resolve_tvdb([], TMDB) == ('none', [])
    foreign = normalize_candidates([{**candidate(3, TMDB), 'images': [{'coverType': 'poster', 'remoteUrl': 'https://evil.test/x.jpg'}]}])
    assert foreign[0]['poster'] is None


class Db:
    def __init__(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("""
            CREATE TABLE portal_accounts(id INTEGER PRIMARY KEY, vodum_user_id INTEGER);
            CREATE TABLE media_users(vodum_user_id INTEGER, type TEXT, external_user_id TEXT);
            CREATE TABLE servers(id INTEGER PRIMARY KEY, type TEXT, url TEXT);
            INSERT INTO portal_accounts VALUES (1, 10);
            INSERT INTO media_users VALUES (10, 'plex', 'p1');
            INSERT INTO servers VALUES (5, 'seerr', 'https://seerr.test');
        """)
        self.conn.execute(jobs.JOB_SCHEMA)

    def query(self, sql, params=()):
        return self.conn.execute(sql, params).fetchall()

    def query_one(self, sql, params=()):
        return self.conn.execute(sql, params).fetchone()

    def execute(self, sql, params=()):
        return self.conn.execute(sql, params)


def uncertain_job(db):
    db.execute("""INSERT INTO portal_media_request_jobs(id, account_id, kind, external_id, title, source, status,
                  result, seerr_server_id, finished_at) VALUES ('j1', 1, 'series', ?, 'Checkerin Marina', 'seerr',
                  'done', 'portal_requests_add_uncertain', 5, CURRENT_TIMESTAMP)""", (TMDB,))


def test_uncertain_job_gets_its_request_id(monkeypatch):
    db = Db()
    uncertain_job(db)
    monkeypatch.setattr(jobs, 'request_options', lambda d: ('seerr', 10))
    monkeypatch.setattr(tracking.media_requests, 'api', FakeSeerr(requests=[existing_request(id=55)]))
    monkeypatch.setattr(tracking, 'seerr_find_user', lambda *a: {'id': 7, 'permissions': 0})
    rows = jobs.media_request_jobs(db, 1, refresh=False)
    assert rows[0]['result'] == 'portal_requests_seerr_unconfirmed'
    found = jobs.media_request_jobs(db, 1)
    stored = db.query_one('SELECT seerr_request_id, result FROM portal_media_request_jobs')
    assert stored['seerr_request_id'] == 55
    assert stored['result'] == 'portal_requests_added'
    assert found[0]['result'] == 'portal_requests_added'


def test_uncertain_job_without_request_stays_and_is_not_retried_at_once(monkeypatch):
    db = Db()
    uncertain_job(db)
    fake = FakeSeerr(requests=[])
    monkeypatch.setattr(jobs, 'request_options', lambda d: ('seerr', 10))
    monkeypatch.setattr(tracking.media_requests, 'api', fake)
    monkeypatch.setattr(tracking, 'seerr_find_user', lambda *a: {'id': 7, 'permissions': 0})
    first = jobs.media_request_jobs(db, 1)
    assert first[0]['result'] == 'portal_requests_seerr_unconfirmed'
    calls = len(fake.paths)
    jobs.media_request_jobs(db, 1)
    assert len(fake.paths) == calls
