from qq_aria2_dl.bot.acl import AclService
from qq_aria2_dl.config import Settings


class DummyOneBot:
    pass


def make_settings(**overrides):
    kwargs = dict(
        _env_file=None,
        download_allowed_users=["1001"],
        admin_users=["9000"],
    )
    kwargs.update(overrides)
    return Settings(**kwargs)


async def test_download_allowed_semantics():
    s = make_settings()
    acl = AclService(s, DummyOneBot(), db=None)
    await acl.init()
    assert acl.is_download_allowed("1001") is True
    assert acl.is_download_allowed(1001) is True
    assert acl.is_download_allowed("9999") is False


async def test_global_admin():
    s = make_settings()
    acl = AclService(s, DummyOneBot(), db=None)
    await acl.init()
    assert acl.is_global_admin("9000") is True
    assert acl.is_global_admin("1001") is False


async def test_add_remove_whitelist_runtime():
    s = make_settings()
    acl = AclService(s, DummyOneBot(), db=None)
    await acl.init()
    await acl.add_allowed_user("2002")
    assert acl.is_download_allowed("2002") is True
    await acl.remove_allowed_user("2002")
    assert acl.is_download_allowed("2002") is False