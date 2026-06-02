"""QuietHTTPServer must NOT set SO_REUSEADDR — a second instance on the
same port should fail loudly, not silently share traffic."""


def test_allow_reuse_address_is_false():
    from server import QuietHTTPServer
    assert QuietHTTPServer.allow_reuse_address is False
