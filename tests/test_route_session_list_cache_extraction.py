"""Import and identity tests for the route_session_list_cache extraction."""


def test_route_session_list_cache_exports():
    from api import route_session_list_cache as slc

    assert hasattr(slc, "_SESSIONS_CACHE")
    assert hasattr(slc, "_session_list_cache_key")
    assert callable(slc._session_list_cache_overlay_runtime_rows)
    assert callable(slc._session_list_cache_source_stamp)


def test_backward_compat_from_routes():
    from api import route_session_list_cache as slc
    import api.routes as routes

    assert hasattr(routes, "_session_list_cache_key")
    assert hasattr(routes, "_session_list_cache_state_db_fingerprint")
    assert callable(routes._session_list_cache_done)
    assert slc._session_list_cache_done is routes._session_list_cache_done


def test_shared_cache_objects():
    from api import route_session_list_cache as slc
    import api.routes as routes

    assert slc._SESSIONS_CACHE is routes._SESSIONS_CACHE
    assert slc._SESSIONS_CACHE_INFLIGHT is routes._SESSIONS_CACHE_INFLIGHT
    assert slc._SESSIONS_CACHE_LOCK is routes._SESSIONS_CACHE_LOCK


def test_shared_cache_state_mutation():
    from api import route_session_list_cache as slc
    import api.routes as routes

    key = routes._session_list_cache_key(
        active_profile="default",
        all_profiles=False,
        show_cli_sessions=False,
        show_previous_messaging_sessions=False,
        show_cron_sessions=False,
    )
    payload = {"sessions": []}
    routes._session_list_cache_clear()
    routes._session_list_cache_set(key, payload)
    try:
        shared_payload, _fresh = slc._session_list_cache_get(key, allow_stale=True)
        assert shared_payload == payload
        assert _fresh in (False, True)
    finally:
        routes._session_list_cache_clear()


def test_live_scalar_exports_follow_route_session_list_cache_state():
    from api import route_session_list_cache as slc
    import api.routes as routes

    before = routes._SESSIONS_CACHE_GLOBAL_INVALIDATION_VERSION
    routes._session_list_cache_clear()
    after = routes._SESSIONS_CACHE_GLOBAL_INVALIDATION_VERSION

    assert after == slc._SESSIONS_CACHE_GLOBAL_INVALIDATION_VERSION
    assert after == before + 1


def test_no_circular_import():
    import pathlib

    src = (
        pathlib.Path(__file__).parent.parent
        / "api"
        / "route_session_list_cache.py"
    ).read_text()
    for line in src.splitlines():
        if line.startswith("from api.routes import") or line.startswith("import api.routes"):
            raise AssertionError(
                "route_session_list_cache must not import api.routes at module scope"
            )
