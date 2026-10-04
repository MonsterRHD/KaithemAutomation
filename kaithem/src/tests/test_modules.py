import copy
import gc
import glob
import io
import json
import os
import shutil
import sys
import time
import weakref
import zipfile

import pytest

if "--collect-only" not in sys.argv:  # pragma: no cover
    from kaithem.api import modules as modulesapi
    from kaithem.api import tags
    from kaithem.src import directories, messagebus, modules, modules_state
    from kaithem.src.plugins import CorePluginEventResources

dir = "/dev/shm/kaithem_tests/"


class testobj:
    pass


def test_make_module():
    n = "test" + str(time.time()).replace(".", "_")

    modules.newModule(n)
    assert n in modules_state.ActiveModules

    # Todo we should probably have a cleaner interface
    # for doing this programmatically

    with modules_state.modulesLock:
        type = "event"
        rt = modules_state.resource_types[type]
        # If create returns None, assume it doesn't want
        #  to insert a module or handles it by itself
        r = rt.on_create_request(n, "testevt", {})
        rt._validate(r)
        if r:
            modules_state.raw_insert_resource(n, "testevt", r)
            rt.on_load(n, "testevt", r)

    assert "testevt" in modules_state.ActiveModules[n]

    # Check file on disk and internal data structure
    assert os.path.exists(os.path.join(dir, "modules/data/" + n))

    assert os.path.exists(os.path.join(dir, "modules/data/", n, "testevt.py"))
    assert "testevt" in modules_state.ActiveModules[n]

    d = dict(
        setup="x = 8\n",
        trigger="x>6",
        action="global x\n\nx= 5",
        priority="interactive",
        ratelimit=1,
        enable=True,
    )

    x = copy.deepcopy(modules_state.ActiveModules[n]["testevt"])
    x.update(d)
    with modules_state.modulesLock:
        modules_state.raw_insert_resource(n, "testevt", x)
        modules.handleResourceChange(n, "testevt")

    assert "x = 8" in str(modules_state.ActiveModules[n]["testevt"])

    assert (n, "testevt") in CorePluginEventResources._events_by_module_resource

    with modulesapi.modules_lock:
        assert "testevt" in modulesapi.list_resources(n)

    x = CorePluginEventResources._events_by_module_resource[(n, "testevt")]

    x.pymodule.__dict__["test_obj"] = testobj()
    ref = weakref.ref(x.pymodule.__dict__["test_obj"])

    # Ensure the event actually worked
    time.sleep(1)
    assert x.pymodule.__dict__["x"] == 5

    with modulesapi.modules_lock:
        modulesapi.delete_resource(n, "testevt")

    assert (
        n,
        "testevt",
    ) not in CorePluginEventResources._events_by_module_resource

    # The scope of the dynamically generated module should be gone now
    assert ref() is None

    type = "tagpoint"

    d = dict(
        tag="test_tag_foo",
        min="",
        max="",
        hi="",
        lo="",
        interval="",
        default=99,
        tag_type="numeric",
    )
    rt = modules_state.resource_types[type]
    r = rt.on_create_request(n, "testtag", d)

    with modules_state.modulesLock:
        modules_state.raw_insert_resource(n, "testtag", r)
        modules.handleResourceChange(n, "testtag")

    assert tags.existing_tag("test_tag_foo").value == 99

    # Round trip upload and download with the YAML mechanism

    old_hash = modules_state.getModuleHash(n)

    old_json = json.dumps(
        modules_state.ActiveModules[n], sort_keys=True, indent=4
    )

    zf = modules_state.getModuleAsYamlZip(n)
    assert zf
    zf2 = io.BytesIO()

    for i in zf:
        zf2.write(i)

    modules.rmModule(n)

    if "/test_tag_foo" in tags.all_tags_raw():
        gc.collect()

    assert "/test_tag_foo" not in tags.all_tags_raw()

    modules.load_modules_from_zip(zf2)

    new_json = json.dumps(
        modules_state.ActiveModules[n], sort_keys=True, indent=4
    )
    if not old_json == new_json:
        print("old_json != new_json")
        print(old_json)
        print("new_json")
        print(new_json)

    assert old_json == new_json

    assert n in modules_state.ActiveModules
    assert "/test_tag_foo" in tags.all_tags_raw()
    assert old_hash == modules_state.hashModule(n)
    assert old_hash == modules_state.getModuleHash(n)

    modules.rmModule(n)


def test_zip_fail_upload():
    zf = io.BytesIO()

    zp = zipfile.ZipFile(zf, "w")
    zp.writestr("failmodule/.fail_to_load_this_module_instantly", b"foo")

    zp.close()
    zf.seek(0)

    with pytest.raises(RuntimeError):
        modules.load_modules_from_zip(zf)

    assert "failmodule" not in modules_state.ActiveModules

    assert "failmodule" not in os.listdir(os.path.join(dir, "modules/data/"))


# ---------------------------------------------------------------------------
# Atomic, recoverable ZIP replacement
# ---------------------------------------------------------------------------


def _unique_name(prefix):
    return prefix + str(time.time()).replace(".", "_")


def _tagpoint_request(module, resname, tag, default):
    rt = modules_state.resource_types["tagpoint"]
    return rt.on_create_request(
        module,
        resname,
        dict(
            tag=tag,
            min="",
            max="",
            hi="",
            lo="",
            interval="",
            default=default,
            tag_type="numeric",
        ),
    )


def _tagpoint_zip_files(resname, tag, default):
    r = _tagpoint_request("placeholder", resname, tag, default)
    return {
        k: v.encode()
        for k, v in modules_state.serializeResource(resname, r).items()
    }


def _zip_with(module_files):
    zf = io.BytesIO()
    with zipfile.ZipFile(zf, "w") as zp:
        for arcname, data in module_files.items():
            zp.writestr(arcname, data)
    zf.seek(0)
    return zf


def _add_live_tagpoint(n, resname, tag, default):
    with modules_state.modulesLock:
        modules_state.raw_insert_resource(
            n, resname, _tagpoint_request(n, resname, tag, default)
        )
        modules.handleResourceChange(n, resname)


def _set_filedata(n, name, data):
    p = modules_state.filename_for_file_resource(n, name)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write(data)
    modules_state.importFiledataFolderStructure(n)


def _get_filedata(n, name):
    with open(modules_state.filename_for_file_resource(n, name), "rb") as f:
        return f.read()


def _assert_tag_eventually(tag, value):
    for i in range(50):
        try:
            if tags.existing_tag(tag).value == value:
                return
        except Exception:
            pass
        time.sleep(0.05)
    assert tags.existing_tag(tag).value == value


class _LifecycleNotes:
    """Records module lifecycle notifications while active."""

    topics = (
        "/system/modules/loaded",
        "/system/modules/unloaded",
        "/system/modules/deleted",
    )

    def __enter__(self):
        self.notes = []
        self.subs = []
        for t in self.topics:

            def cb(topic, value):
                self.notes.append((topic, value))

            messagebus.subscribe(t, cb)
            self.subs.append((t, cb))
        return self

    def __exit__(self, *a):
        for t, cb in self.subs:
            messagebus.unsubscribe(t, cb)

    def settle(self):
        # Notifications are dispatched on worker threads.
        time.sleep(0.5)


def test_zip_replace_atomic_success():
    n = _unique_name("atomic_ok_")
    tag = n + "_tag"
    try:
        modules.newModule(n)
        _add_live_tagpoint(n, "oldres", tag, 41)
        _set_filedata(n, "hello.txt", b"old-filedata")
        _assert_tag_eventually(tag, 41)

        new_files = {
            f"{n}/{k}": v
            for k, v in _tagpoint_zip_files("newres", tag, 42).items()
        }
        new_files[f"{n}/__filedata__/hello.txt"] = b"new-filedata"

        modules.load_modules_from_zip(_zip_with(new_files), replace=True)

        assert "oldres" not in modules_state.ActiveModules[n]
        assert "newres" in modules_state.ActiveModules[n]
        _assert_tag_eventually(tag, 42)
        assert _get_filedata(n, "hello.txt") == b"new-filedata"
        assert not glob.glob(
            os.path.join(modules_state.getModuleDir(n), "oldres*")
        )

        modules_state.recalcModuleHashes()
        assert modules_state.getModuleHash(n) == modules_state.hashModule(n)

        # No transaction debris may survive a successful commit.
        assert not os.path.exists(modules._upload_txn_root())
        assert not os.path.exists(
            os.path.join(directories.moduledir, "__backup__")
        )
    finally:
        if n in modules_state.ActiveModules:
            modules.rmModule(n)


def test_zip_replace_validation_failure_keeps_old():
    n = _unique_name("atomic_bad_")
    tag = n + "_tag"
    try:
        modules.newModule(n)
        _add_live_tagpoint(n, "oldres", tag, 41)
        _set_filedata(n, "hello.txt", b"old-filedata")
        before = json.dumps(
            modules_state.ActiveModules[n], sort_keys=True, default=str
        )

        new_files = {
            f"{n}/{k}": v
            for k, v in _tagpoint_zip_files("newres", tag, 42).items()
        }
        new_files[f"{n}/.fail_to_load_this_module_instantly"] = b"x"

        with _LifecycleNotes() as notes:
            with pytest.raises(RuntimeError):
                modules.load_modules_from_zip(
                    _zip_with(new_files), replace=True
                )
            notes.settle()
            # The old version kept serving: no teardown/load noise.
            assert notes.notes == []

        assert (
            json.dumps(
                modules_state.ActiveModules[n],
                sort_keys=True,
                default=str,
            )
            == before
        )
        assert "oldres" in modules_state.ActiveModules[n]
        assert "newres" not in modules_state.ActiveModules[n]
        _assert_tag_eventually(tag, 41)
        assert _get_filedata(n, "hello.txt") == b"old-filedata"
        assert not os.path.exists(modules._upload_txn_root())
    finally:
        if n in modules_state.ActiveModules:
            modules.rmModule(n)


def test_zip_replace_init_failure_rolls_back(monkeypatch):
    n = _unique_name("atomic_initfail_")
    tag = n + "_tag"
    try:
        modules.newModule(n)
        _add_live_tagpoint(n, "oldres", tag, 41)
        _set_filedata(n, "hello.txt", b"old-filedata")
        before = json.dumps(
            modules_state.ActiveModules[n], sort_keys=True, default=str
        )

        calls = [0]
        real_bookkeep = modules.bookkeeponemodule

        def fail_first_bookkeep(module, *a, **k):
            calls[0] += 1
            if calls[0] == 1:
                raise RuntimeError("simulated init failure")
            return real_bookkeep(module, *a, **k)

        monkeypatch.setattr(modules, "bookkeeponemodule", fail_first_bookkeep)

        new_files = {
            f"{n}/{k}": v
            for k, v in _tagpoint_zip_files("newres", tag, 42).items()
        }
        new_files[f"{n}/__filedata__/hello.txt"] = b"new-filedata"

        with _LifecycleNotes() as notes:
            with pytest.raises(RuntimeError, match="simulated init failure"):
                modules.load_modules_from_zip(
                    _zip_with(new_files), replace=True
                )
            notes.settle()
            assert notes.notes == []

        assert (
            json.dumps(
                modules_state.ActiveModules[n],
                sort_keys=True,
                default=str,
            )
            == before
        )
        assert "oldres" in modules_state.ActiveModules[n]
        assert "newres" not in modules_state.ActiveModules[n]
        _assert_tag_eventually(tag, 41)
        assert _get_filedata(n, "hello.txt") == b"old-filedata"
        assert not os.path.exists(modules._upload_txn_root())
    finally:
        if n in modules_state.ActiveModules:
            modules.rmModule(n)


def test_zip_replace_rejects_locked_module():
    n = _unique_name("atomic_locked_")
    tag = n + "_tag"
    try:
        modules.newModule(n)
        _add_live_tagpoint(n, "oldres", tag, 41)
        with modules_state.modulesLock:
            modules_state.ActiveModules[n]["__metadata__"]["module_lock"] = True

        new_files = {
            f"{n}/{k}": v
            for k, v in _tagpoint_zip_files("newres", tag, 42).items()
        }
        with pytest.raises(PermissionError):
            modules.load_modules_from_zip(_zip_with(new_files), replace=True)

        assert "oldres" in modules_state.ActiveModules[n]
        _assert_tag_eventually(tag, 41)
        assert not os.path.exists(modules._upload_txn_root())
    finally:
        if n in modules_state.ActiveModules:
            modules.rmModule(n)


def test_zip_replace_requires_replace_flag():
    n = _unique_name("atomic_noreplace_")
    tag = n + "_tag"
    try:
        modules.newModule(n)
        _add_live_tagpoint(n, "oldres", tag, 41)

        new_files = {
            f"{n}/{k}": v
            for k, v in _tagpoint_zip_files("newres", tag, 42).items()
        }
        with pytest.raises(RuntimeError, match="already loaded"):
            modules.load_modules_from_zip(_zip_with(new_files))

        assert "oldres" in modules_state.ActiveModules[n]
        assert "newres" not in modules_state.ActiveModules[n]
    finally:
        if n in modules_state.ActiveModules:
            modules.rmModule(n)


def test_zip_replace_rejects_external_module():
    n = _unique_name("atomic_external_")
    tag = n + "_tag"
    try:
        modules.newModule(n)
        _add_live_tagpoint(n, "oldres", tag, 41)
        modules_state.external_module_locations[n] = "/nonexistent/location"

        new_files = {
            f"{n}/{k}": v
            for k, v in _tagpoint_zip_files("newres", tag, 42).items()
        }
        try:
            with pytest.raises(RuntimeError, match="external module"):
                modules.load_modules_from_zip(
                    _zip_with(new_files), replace=True
                )
        finally:
            modules_state.external_module_locations.pop(n, None)

        assert "oldres" in modules_state.ActiveModules[n]
        assert "newres" not in modules_state.ActiveModules[n]
        assert not os.path.exists(modules._upload_txn_root())
    finally:
        if n in modules_state.ActiveModules:
            modules.rmModule(n)


def test_zip_multi_module_independent_commit():
    ok = _unique_name("multi_a_ok_")
    bad = _unique_name("multi_z_bad_")
    tag = ok + "_tag"
    files = {}
    for k, v in _tagpoint_zip_files("r", tag, 7).items():
        files[f"{ok}/{k}"] = v
    files[f"{bad}/.fail_to_load_this_module_instantly"] = b"x"

    try:
        with pytest.raises(RuntimeError):
            modules.load_modules_from_zip(_zip_with(files))

        # Earlier module committed and survives the later failure.
        assert ok in modules_state.ActiveModules
        assert "r" in modules_state.ActiveModules[ok]
        _assert_tag_eventually(tag, 7)

        # Failed module left nothing behind.
        assert bad not in modules_state.ActiveModules
        assert bad not in os.listdir(os.path.join(dir, "modules/data/"))
        assert not os.path.exists(modules._upload_txn_root())
    finally:
        if ok in modules_state.ActiveModules:
            modules.rmModule(ok)
        if bad in modules_state.ActiveModules:
            modules.rmModule(bad)


def test_recover_interrupted_replace_restores_old():
    n = _unique_name("recover_replace_")
    tag = n + "_tag"
    live = os.path.join(directories.moduledir, "data", n)
    try:
        modules.newModule(n)
        _add_live_tagpoint(n, "oldres", tag, 41)
        _set_filedata(n, "hello.txt", b"old-filedata")

        tdir = os.path.join(
            modules._upload_txn_root(), "upload-simulated-crash"
        )
        os.makedirs(os.path.join(tdir, "old"))
        shutil.copytree(live, os.path.join(tdir, "old", n))
        modules.rmModule(n)

        # Simulate a half-finished new version sitting in data/.
        os.makedirs(live)
        with open(os.path.join(live, "partial-new.txt"), "w") as f:
            f.write("partial")
        with open(os.path.join(live, "junk.yaml"), "w") as f:
            f.write("garbage: ::")

        modules._atomic_write_json(
            modules._txn_marker_path(tdir, n),
            {"version": 1, "module": n},
        )

        modules.recover_pending_module_uploads()

        assert not os.path.exists(tdir)
        assert not os.path.exists(os.path.join(live, "partial-new.txt"))
        assert not glob.glob(os.path.join(modules._upload_txn_root(), "*"))
        assert _get_filedata(n, "hello.txt") == b"old-filedata"

        # As at real startup, recovery is followed by a normal load.
        modules.loadModule(live, n)
        modules.bookkeeponemodule(n)
        assert "oldres" in modules_state.ActiveModules[n]
        _assert_tag_eventually(tag, 41)
    finally:
        if n in modules_state.ActiveModules:
            modules.rmModule(n)


def test_recover_interrupted_new_upload_is_discarded():
    n = _unique_name("recover_new_")
    data = os.path.join(directories.moduledir, "data", n)
    tdir = os.path.join(modules._upload_txn_root(), "upload-simulated-crash")
    os.makedirs(data)
    os.makedirs(tdir)
    with open(os.path.join(data, "partial.txt"), "w") as f:
        f.write("partial")
    modules._atomic_write_json(
        modules._txn_marker_path(tdir, n), {"version": 1, "module": n}
    )

    modules.recover_pending_module_uploads()

    assert not os.path.exists(data)
    assert not os.path.exists(tdir)
    assert n not in modules_state.ActiveModules


def test_recover_legacy_backup_folder():
    n = _unique_name("recover_legacy_")
    tag = n + "_tag"
    data = os.path.join(directories.moduledir, "data", n)
    backup = os.path.join(directories.moduledir, "__backup__", n)

    os.makedirs(backup)
    for k, v in _tagpoint_zip_files("r", tag, 9).items():
        with open(os.path.join(backup, k), "wb") as f:
            f.write(v)

    # A half-written new version occupies the live location.
    os.makedirs(data)
    with open(os.path.join(data, "partial.txt"), "w") as f:
        f.write("partial")

    try:
        modules.recover_pending_module_uploads()

        assert not os.path.exists(
            os.path.join(directories.moduledir, "__backup__")
        )
        assert not os.path.exists(os.path.join(data, "partial.txt"))

        modules.loadModule(data, n)
        modules.bookkeeponemodule(n)
        assert "r" in modules_state.ActiveModules[n]
        _assert_tag_eventually(tag, 9)
    finally:
        if n in modules_state.ActiveModules:
            modules.rmModule(n)
