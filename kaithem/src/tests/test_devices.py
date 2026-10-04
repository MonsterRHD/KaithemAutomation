import gc
import json
import os
import sys
import time

import pytest
import yaml

if "--collect-only" not in sys.argv:  # pragma: no cover
    from kaithem.src import quart_app

    dir = "/dev/shm/kaithem_tests/"

    tc = quart_app.app.test_client()


# def test_error_in_device():
#     from scullery import messagebus

#     from kaithem.src import (
#         devices,
#         devices_interface,
#         modules,
#         modules_state,
#     )

#     n = "test" + str(time.time()).replace(".", "_")

#     modules.newModule(n)

#     assert n in modules_state.ActiveModules

#     devices_interface.create_device_from_kwargs(
#         module=n, resource="devtest", type="DemoDevice", name="pytest_demo2"
#     )

#     got_errs = [0]

#     def message_handler(toppic, data: str):
#         data = data.lower()
#         if "pytest_demo2" in data:
#             if "error" in data:
#                 got_errs[0] += 1

#     messagebus.subscribe("/system/notifications/errors", message_handler)

#     assert "pytest_demo2" in devices.devices_host.get_devices()

#     d = devices.devices_host.devices["pytest_demo2"]
#     d.set_data_point("test_error", 1)
#     time.sleep(0.5)
#     assert got_errs[0] == 1

#     # Only the first one goes to the notifications
#     d.set_data_point("test_error", 2)
#     time.sleep(0.5)
#     assert got_errs[0] == 1

#     modules.rmResource(n, "devtest")


def test_subdevice_loading():
    from kaithem.src import (
        devices,
        modules,
        modules_state,
    )

    n = "test" + str(time.time()).replace(".", "_")
    modules.newModule(n)
    assert n in modules_state.ActiveModules

    devcon_child = {
        "resource": {"type": "device", "modified": 1777171067},
        "device": {
            "type": "DemoDevice",
            "name": "pytest_demo_parent/subdevice",
            "is_subdevice": True,
            "device.echo_number": 81,
        },
    }

    modules.createResource(n, "devtest", devcon_child)

    # Does't load till parent makes it
    assert "pytest_demo_parent/subdevice" not in devices.devices_host.devices
    assert "pytest_demo_parent/subdevice" in devices.device_data_cache

    devcon = {
        "resource": {"type": "device", "modified": 1777171067},
        "device": {
            "type": "DemoDevice",
            "name": "pytest_demo_parent",
        },
    }

    modules.createResource(n, "devtest2", devcon)

    # System should look up the config when the device is made
    # TODO test init where they both get made at once
    dev = devices.devices_host.devices["pytest_demo_parent/subdevice"]
    assert dev.wait_device_ready().config["device.echo_number"] == 81


def test_device_alerts():
    from kaithem.src import (
        alerts,
        devices,
        devices_interface,
        modules,
        modules_state,
    )

    n = "test" + str(time.time()).replace(".", "_")

    modules.newModule(n)

    assert n in modules_state.ActiveModules

    devices_interface.create_blank_device(
        module=n, resource="devtest", type="DemoDevice", name="pytest_demo2"
    )

    d = devices.devices_host.devices["pytest_demo2"].device
    assert d
    d.set_alarm("testalert137", "random", "value>0", priority="info")

    found = False
    time.sleep(0.3)
    for i in alerts.all:
        j = alerts.all[i]
        if "testalert137" in j.name:
            if j.sm.state == "normal":
                continue
            if j.priority == "info":
                found = True
    assert found

    devices_interface.delete_device("pytest_demo2")

    found = False
    time.sleep(0.3)
    for i in alerts.all:
        j = alerts.all[i]
        if "testalert137" in j.name:
            found = True

    assert not found


def test_make_demo_device():
    from kaithem.src import (
        devices,
        devices_interface,
        modules,
        modules_state,
        tagpoints,
    )

    n = "test" + str(time.time()).replace(".", "_")

    modules.newModule(n)

    assert n in modules_state.ActiveModules

    devices_interface.create_blank_device(
        module=n, resource="devtest", type="DemoDevice", name="pytest_demo"
    )

    assert "pytest_demo" in devices.devices_host.devices
    assert "pytest_demo" in devices.devices_host.get_devices()

    assert tagpoints.allTagsAtomic["/devices/pytest_demo.random"]().value
    assert tagpoints.allTagsAtomic["/devices/pytest_demo.random"]().value < 1

    assert tagpoints.allTagsAtomic[
        "/devices/pytest_demo/subdevice.random"
    ]().value

    devices.updateDevice(
        "pytest_demo",
        {
            "temp.kaithem.store_in_module": n,
            "temp.kaithem.store_in_resource": "devtest",
            "json": """{
                "device.fixed_number_multiplier": 10000909000,
                "type": "DemoDevice"
            }""",
        },
    )

    assert os.path.exists(os.path.join(dir, "modules/data/", n, "devtest.yaml"))

    with open(os.path.join(dir, "modules/data/", n, "devtest.yaml")) as f:
        lr = f.read()

    assert "10000909000" in lr

    assert (
        str(
            yaml.load(lr, yaml.SafeLoader)["device"][
                "device.fixed_number_multiplier"
            ]
        )
        == "10000909000"
    )

    rsc_data = modules_state.ActiveModules[n]["devtest"]

    # Various corruptions where sections get mixed
    assert "device" not in rsc_data["device"]
    assert "device.fixed_number_multiplier" not in rsc_data
    assert "resource" not in rsc_data["device"]

    assert (
        devices.devices_host.devices["pytest_demo"].config[
            "device.fixed_number_multiplier"
        ]
        == 10000909000
    )

    assert tagpoints.allTagsAtomic["/devices/pytest_demo.random"]().value

    assert tagpoints.allTagsAtomic["/devices/pytest_demo.random"]().value > 100

    devices.updateDevice(
        "pytest_demo/subdevice",
        {
            "temp.kaithem.store_in_module": n,
            "temp.kaithem.store_in_resource": "devtest_subdevice",
            "json": """{
                "device.echo_number": "6",
                "type": "DemoDevice"
            }""",
        },
    )

    assert (
        devices.devices_host.devices["pytest_demo/subdevice"].config[
            "device.echo_number"
        ]
        == "6"
    )

    devices_interface.delete_device("pytest_demo")

    gc.collect()
    gc.collect()
    time.sleep(0.2)

    assert "/devices/pytest_demo.random" not in tagpoints.allTags
    assert (
        "/devices/pytest_demo/subdevice.random" not in tagpoints.allTagsAtomic
    )

    assert "pytest_demo" not in devices.devices_host.devices
    assert "pytest_demo" not in devices.devices_host.get_devices()
    assert "pytest_demo/subdevice" not in devices.devices_host.devices

    assert "pytest_demo/subdevice" in devices.device_data_cache

    # Remake it
    devices_interface.create_blank_device(
        module=n, resource="devtest", type="DemoDevice", name="pytest_demo"
    )

    # Ensure it picks up config from the module
    assert (
        devices.devices_host.devices["pytest_demo/subdevice"].config[
            "device.echo_number"
        ]
        == "6"
    )

    devices_interface.delete_device("pytest_demo")

    assert "pytest_demo" not in devices.devices_host.devices
    assert "pytest_demo" not in devices.devices_host.get_devices()

    modules.rmResource(n, "devtest_subdevice")

    assert "pytest_demo/subdevice" not in devices.device_data_cache

    assert not os.path.exists(
        os.path.join(dir, "modules/data/", n, "devtest.yaml")
    )


# ---------------------------------------------------------------------------
# Atomic updateDevice() tests: validation first, one switch, rollback on any
# failure so there are never ghost tags, duplicate devices or orphaned
# parent/child relationships.
# ---------------------------------------------------------------------------


def _yaml_path(module, resource):
    return os.path.join(
        dir, "modules", "data", module, resource + ".yaml"
    )


def _conf_folder(module, resource):
    from kaithem.src import devices

    return devices.get_config_folder_from_info(
        module, resource, None, create=False, always_return=True
    )


def _settle_gc():
    gc.collect()
    time.sleep(0.15)
    gc.collect()


def _wait_for(condition, timeout=2.0):
    # Device add/remove events are dispatched on worker threads.
    deadline = time.time() + timeout
    while time.time() < deadline:
        if condition():
            return True
        time.sleep(0.05)
    return condition()


def _watch_device_events(prefix):
    from scullery import messagebus

    seen = []

    def added(topic, value):
        if value.startswith(prefix):
            seen.append(("added", value))

    def removed(topic, value):
        if value.startswith(prefix):
            seen.append(("removed", value))

    messagebus.subscribe("/devices/added/", added)
    messagebus.subscribe("/devices/removed/", removed)
    return seen


def _make_parent_with_configured_sub(module, base, parent_resource="atomdev"):
    """Create a DemoDevice plus a saved (configured) subdevice whose
    echo_number is customized, so tests can prove child config migrates."""
    from kaithem.src import devices, devices_interface

    devices_interface.create_blank_device(
        module=module,
        resource=parent_resource,
        type="DemoDevice",
        name=base,
    )
    sub = base + "/subdevice"
    # Device resource names may not contain dots, so configured
    # subdevices live at an ordinary (custom) resource path.
    sub_res = "atomsub"
    devices.updateDevice(
        sub,
        {
            "temp.kaithem.store_in_module": module,
            "temp.kaithem.store_in_resource": sub_res,
            "json": json.dumps(
                {"type": "DemoDevice", "device.echo_number": 9}
            ),
        },
    )
    assert devices.devices_host.devices[sub].config["device.echo_number"] == 9
    return sub, sub_res


def _update(base, module, resource, **config):
    from kaithem.src import devices

    devices.updateDevice(
        base,
        {
            "temp.kaithem.store_in_module": module,
            "temp.kaithem.store_in_resource": resource,
            "json": json.dumps(config),
        },
    )


def test_atomic_update_invalid_json_changes_nothing():
    from kaithem.src import devices, devices_interface, modules, tagpoints

    n = "test" + str(time.time_ns())
    modules.newModule(n)
    base = "pytest_atom_badjson_" + str(time.time_ns())
    devices_interface.create_blank_device(
        module=n, resource="atomdev", type="DemoDevice", name=base
    )
    seen = _watch_device_events(base)

    with pytest.raises(json.JSONDecodeError):
        devices.updateDevice(
            base,
            {
                "temp.kaithem.store_in_module": n,
                "temp.kaithem.store_in_resource": "atomdev2",
                "json": "{this is not json",
            },
        )

    _settle_gc()
    assert base in devices.devices_host.devices
    assert base + "/subdevice" in devices.devices_host.devices
    assert tagpoints.allTagsAtomic[f"/devices/{base}.random"]()
    assert os.path.exists(_yaml_path(n, "atomdev"))
    assert not os.path.exists(_yaml_path(n, "atomdev2"))
    assert seen == []

    devices_interface.delete_device(base)


def test_atomic_update_same_name_emits_no_events():
    from kaithem.src import devices, devices_interface, modules

    n = "test" + str(time.time_ns())
    modules.newModule(n)
    base = "pytest_atom_samename_" + str(time.time_ns())
    _make_parent_with_configured_sub(n, base)
    seen = _watch_device_events(base)

    _update(
        base,
        n,
        "atomdev",
        **{
            "name": base,
            "type": "DemoDevice",
            "device.fixed_number_multiplier": 11,
        },
    )

    assert (
        devices.devices_host.devices[base].config[
            "device.fixed_number_multiplier"
        ]
        == 11
    )
    assert (
        devices.devices_host.devices[base + "/subdevice"].config[
            "device.echo_number"
        ]
        == 9
    )
    assert seen == []

    devices_interface.delete_device(base)


def test_atomic_rename_moves_whole_tree():
    from kaithem.src import devices, devices_interface, modules, tagpoints

    n = "test" + str(time.time_ns())
    modules.newModule(n)
    base = "pytest_atom_ren_" + str(time.time_ns())
    base2 = base + "_new"
    sub, sub_res = _make_parent_with_configured_sub(n, base)
    sub2 = base2 + "/subdevice"
    # Same module: the child keeps its custom resource path.
    sub_res2 = sub_res
    seen = _watch_device_events(base)

    # Put a marker in the parent and subdevice config folders, it must
    # travel with the move and not be copied back or left half behind.
    pfolder = _conf_folder(n, "atomdev")
    with open(os.path.join(pfolder, "marker.txt"), "w") as f:
        f.write("x")
    sfolder = _conf_folder(n, sub_res)
    with open(os.path.join(sfolder, "submarker.txt"), "w") as f:
        f.write("y")

    _update(
        base,
        n,
        "atomdev2",
        **{
            "name": base2,
            "type": "DemoDevice",
            "device.fixed_number_multiplier": 7,
        },
    )
    _settle_gc()

    # Runtime: new tree only, configs preserved
    assert base not in devices.devices_host.devices
    assert sub not in devices.devices_host.devices
    assert base2 in devices.devices_host.devices
    assert sub2 in devices.devices_host.devices
    assert (
        devices.devices_host.devices[base2].config[
            "device.fixed_number_multiplier"
        ]
        == 7
    )
    assert (
        devices.devices_host.devices[sub2].config["device.echo_number"] == 9
    )

    # Tags and alerts carry the new names only
    assert f"/devices/{base}.random" not in tagpoints.allTags
    assert tagpoints.allTagsAtomic[f"/devices/{base2}.random"]()
    assert (
        f"/devices/{sub}.echo_number"
        not in tagpoints.allTagsAtomic
    )
    assert tagpoints.allTagsAtomic[f"/devices/{sub2}.echo_number"]()

    # No old names survive in the config cache
    assert base not in devices.device_data_cache
    assert sub not in devices.device_data_cache
    for k, (mm, rr, cfg) in devices.device_data_cache.items():
        assert not k.startswith(base + "/")
        if k.startswith(base2):
            assert (mm, rr) in (
                (n, "atomdev2"),
                (n, sub_res2),
            )

    # Module resources: parent moves, child resource path is kept while
    # its stored device name is rewritten.
    am = modules.modules_state.ActiveModules[n]
    assert "atomdev2" in am
    assert sub_res2 in am
    assert "atomdev" not in am
    assert am[sub_res2]["device"]["name"] == sub2

    # Disk: only the new parent file plus the (rewritten) child file
    assert os.path.exists(_yaml_path(n, "atomdev2"))
    assert os.path.exists(_yaml_path(n, sub_res2))
    assert not os.path.exists(_yaml_path(n, "atomdev"))

    # Parent config folder moves; child folder stays with its resource;
    # no staging leftovers anywhere.
    new_pfolder = _conf_folder(n, "atomdev2")
    same_sfolder = _conf_folder(n, sub_res2)
    assert os.path.exists(os.path.join(new_pfolder, "marker.txt"))
    assert os.path.exists(os.path.join(same_sfolder, "submarker.txt"))
    assert not os.path.exists(pfolder)
    assert os.path.exists(sfolder)
    leftovers = [
        x
        for x in os.listdir(
            os.path.join(dir, "modules", "data", n, "__filedata__")
        )
        if ".staging-" in x or ".bak-" in x or ".aside-" in x
    ]
    assert leftovers == []

    # Events match the final visible set, removals announced first
    assert _wait_for(lambda: len(seen) >= 4)
    added = sorted(v for kind, v in seen if kind == "added")
    removed = sorted(v for kind, v in seen if kind == "removed")
    assert added == sorted([base2, sub2])
    assert removed == sorted([base, sub])
    assert [kind for kind, v in seen].index("removed") == 0

    devices_interface.delete_device(base2)


def test_atomic_move_to_other_module():
    from kaithem.src import devices, devices_interface, modules

    n1 = "test" + str(time.time_ns())
    n2 = n1 + "_dst"
    modules.newModule(n1)
    modules.newModule(n2)
    base = "pytest_atom_xmod_" + str(time.time_ns())
    _make_parent_with_configured_sub(n1, base)

    # Move the subdevice to a custom (non default) resource first.
    devices.updateDevice(
        base + "/subdevice",
        {
            "temp.kaithem.store_in_module": n1,
            "temp.kaithem.store_in_resource": "customsub",
            "json": json.dumps(
                {"type": "DemoDevice", "device.echo_number": 9}
            ),
        },
    )

    seen = _watch_device_events(base)
    _update(
        base,
        n2,
        "atomdev",
        **{
            "name": base,
            "type": "DemoDevice",
            "device.fixed_number_multiplier": 13,
        },
    )
    _settle_gc()

    sub = base + "/subdevice"
    assert devices.devices_host.devices[base].config[
        "device.fixed_number_multiplier"
    ] == 13
    assert (
        devices.devices_host.devices[sub].config["device.echo_number"] == 9
    )

    am1 = modules.modules_state.ActiveModules[n1]
    am2 = modules.modules_state.ActiveModules[n2]
    assert "atomdev" not in am1
    assert "customsub" not in am1
    assert "atomdev" in am2
    # The child keeps its relative resource path inside the new module.
    assert "customsub" in am2
    assert am2["customsub"]["device"]["name"] == sub

    # No ghost resource anywhere in the old module still naming the tree.
    old_named = [
        rr
        for rr, rd in am1.items()
        if rd.get("resource", {}).get("type") == "device"
        and str(rd.get("device", {}).get("name", "")).startswith(
            base + "/"
        )
    ]
    assert old_named == []
    assert base not in [
        rd.get("device", {}).get("name") for rd in am1.values()
    ]

    assert not os.path.exists(_yaml_path(n1, "atomdev"))
    assert not os.path.exists(_yaml_path(n1, "customsub"))
    assert os.path.exists(_yaml_path(n2, "atomdev"))
    assert os.path.exists(_yaml_path(n2, "customsub"))

    fd1 = os.path.join(dir, "modules", "data", n1, "__filedata__")
    fd2 = os.path.join(dir, "modules", "data", n2, "__filedata__")
    assert not os.path.exists(os.path.join(fd1, "atomdev.config.d"))
    assert not os.path.exists(os.path.join(fd1, "customsub.config.d"))
    assert os.path.exists(os.path.join(fd2, "atomdev.config.d"))
    assert os.path.exists(os.path.join(fd2, "customsub.config.d"))

    # Same device names before and after -> no add/remove events.
    assert seen == []

    devices_interface.delete_device(base)


def test_atomic_update_plugin_init_failure_rolls_back(monkeypatch):
    from iot_devices.devices.demo import DemoDevice

    from kaithem.src import devices, devices_interface, modules, tagpoints

    n = "test" + str(time.time_ns())
    modules.newModule(n)
    base = "pytest_atom_initfail_" + str(time.time_ns())
    base2 = base + "_v2"
    sub, sub_res = _make_parent_with_configured_sub(n, base)
    seen = _watch_device_events(base)

    pfolder = _conf_folder(n, "atomdev")
    with open(os.path.join(pfolder, "marker.txt"), "w") as f:
        f.write("keepme")

    real_init = DemoDevice.__init__

    def boom(self, data, **kw):
        if data.get("name") == base2:
            raise RuntimeError("synthetic init failure")
        return real_init(self, data, **kw)

    monkeypatch.setattr(DemoDevice, "__init__", boom)

    with pytest.raises(RuntimeError, match="synthetic init failure"):
        _update(
            base,
            n,
            "atomdev2",
            **{
                "name": base2,
                "type": "DemoDevice",
                "device.fixed_number_multiplier": 7,
            },
        )

    _settle_gc()

    # Original device and child relationship restored
    assert base in devices.devices_host.devices
    assert sub in devices.devices_host.devices
    assert base2 not in devices.devices_host.devices
    assert base2 + "/subdevice" not in devices.devices_host.devices
    assert (
        devices.devices_host.devices[sub].config["device.echo_number"] == 9
    )
    assert devices.devices_host.devices[base].device.device_type == "DemoDevice"

    # Tags under the old names are still live
    assert tagpoints.allTagsAtomic[f"/devices/{base}.random"]()
    assert f"/devices/{base2}.random" not in tagpoints.allTagsAtomic

    # Cache carries no new names
    assert base2 not in devices.device_data_cache
    assert base2 + "/subdevice" not in devices.device_data_cache

    # Disk and ActiveModules hold exactly the old version
    am = modules.modules_state.ActiveModules[n]
    assert "atomdev" in am
    assert sub_res in am
    assert "atomdev2" not in am
    assert base2 + ".d" not in am
    assert os.path.exists(_yaml_path(n, "atomdev"))
    assert not os.path.exists(_yaml_path(n, "atomdev2"))
    assert os.path.exists(os.path.join(pfolder, "marker.txt"))
    new_pfolder = _conf_folder(n, "atomdev2")
    assert not os.path.exists(new_pfolder)
    leftovers = [
        x
        for x in os.listdir(
            os.path.join(dir, "modules", "data", n, "__filedata__")
        )
        if ".staging-" in x or ".bak-" in x or ".aside-" in x
    ]
    assert leftovers == []

    # Disk must converge to exactly one (the old) version on restart:
    # scan every device resource yaml in the module directory.
    on_disk_names: dict[str, list[str]] = {}
    mdir = os.path.join(dir, "modules", "data", n)
    for root, _dirs, files in os.walk(mdir):
        for f in files:
            if not f.endswith(".yaml"):
                continue
            with open(os.path.join(root, f)) as fh:
                doc = yaml.safe_load(fh)
            if isinstance(doc, dict) and isinstance(
                doc.get("device"), dict
            ):
                on_disk_names.setdefault(
                    doc["device"].get("name"), []
                ).append(f)
    assert base in on_disk_names
    assert sub in on_disk_names
    assert base2 not in on_disk_names
    assert all(
        k == base or k.startswith(base + "/") for k in on_disk_names
    )
    assert all(len(v) == 1 for v in on_disk_names.values())

    assert seen == []

    devices_interface.delete_device(base)


def test_atomic_update_config_copy_failure_changes_nothing(monkeypatch):
    import shutil as _shutil

    from kaithem.src import devices, devices_interface, modules

    n = "test" + str(time.time_ns())
    modules.newModule(n)
    base = "pytest_atom_copyfail_" + str(time.time_ns())
    _make_parent_with_configured_sub(n, base)
    seen = _watch_device_events(base)

    def boom(src, dst, *a, **k):
        raise RuntimeError("synthetic copy failure")

    monkeypatch.setattr(_shutil, "copytree", boom)

    with pytest.raises(RuntimeError, match="synthetic copy failure"):
        _update(
            base,
            n,
            "atomdev2",
            name=base + "_new",
            type="DemoDevice",
        )

    assert base in devices.devices_host.devices
    assert base + "/subdevice" in devices.devices_host.devices
    assert os.path.exists(_yaml_path(n, "atomdev"))
    assert not os.path.exists(_yaml_path(n, "atomdev2"))
    fd = os.path.join(dir, "modules", "data", n, "__filedata__")
    assert not [x for x in os.listdir(fd) if ".staging-" in x]
    assert seen == []

    devices_interface.delete_device(base)


def test_atomic_update_disk_write_failure_rolls_back(monkeypatch):
    from kaithem.src import (
        devices,
        devices_interface,
        modules,
        modules_state,
    )

    n = "test" + str(time.time_ns())
    modules.newModule(n)
    base = "pytest_atom_writefail_" + str(time.time_ns())
    _make_parent_with_configured_sub(n, base)
    seen = _watch_device_events(base)

    real_save = modules_state.save_resource

    def boom(module, resource, resourceData, name=None):
        if (
            resourceData.get("device", {}).get(
                "device.fixed_number_multiplier"
            )
            == 12345
        ):
            raise OSError("synthetic disk failure")
        return real_save(module, resource, resourceData, name)

    monkeypatch.setattr(modules_state, "save_resource", boom)

    with pytest.raises(OSError, match="synthetic disk failure"):
        _update(
            base,
            n,
            "atomdev",
            **{
                "name": base,
                "type": "DemoDevice",
                "device.fixed_number_multiplier": 12345,
            },
        )

    _settle_gc()

    assert base in devices.devices_host.devices
    assert base + "/subdevice" in devices.devices_host.devices
    assert (
        devices.devices_host.devices[base].config[
            "device.fixed_number_multiplier"
        ]
        != 12345
    )
    with open(_yaml_path(n, "atomdev")) as f:
        assert "12345" not in f.read()
    assert seen == []

    devices_interface.delete_device(base)


def test_atomic_update_rejects_conflicts_and_locks():
    from kaithem.src import (
        devices,
        devices_interface,
        modules,
        modules_state,
    )

    n = "test" + str(time.time_ns())
    modules.newModule(n)
    base = "pytest_atom_conflict_" + str(time.time_ns())
    _make_parent_with_configured_sub(n, base)
    seen = _watch_device_events(base)

    # Device type change is refused up front.
    with pytest.raises(RuntimeError, match="Expected DemoDevice"):
        _update(
            base,
            n,
            "atomdev",
            name=base, type="GPIODevice",
        )
    assert base in devices.devices_host.devices

    # A name already used by another live device is refused.
    other = base + "peer"
    devices_interface.create_blank_device(
        module=n, resource="otherdev", type="DemoDevice", name=other
    )
    with pytest.raises(RuntimeError, match="already exists"):
        _update(
            base,
            n,
            "atomdev2",
            name=other, type="DemoDevice",
        )
    assert base in devices.devices_host.devices
    assert other in devices.devices_host.devices
    devices_interface.delete_device(other)
    # The peer's own create/delete events are expected; the aborted
    # rename must not have added any.
    _settle_gc()
    seen.clear()

    # Resource lock on the existing target keeps the old device.
    modules_state.ActiveModules[n]["atomdev"]["resource"]["lock"] = True
    with pytest.raises(PermissionError):
        _update(
            base,
            n,
            "atomdev",
            name=base, type="DemoDevice",
        )
    del modules_state.ActiveModules[n]["atomdev"]["resource"]["lock"]

    # Module lock (only enforced when the target resource exists).
    modules_state.ActiveModules[n]["__metadata__"] = {"module_lock": True}
    with pytest.raises(PermissionError):
        _update(
            base,
            n,
            "atomdev",
            name=base, type="DemoDevice",
        )
    del modules_state.ActiveModules[n]["__metadata__"]

    assert base in devices.devices_host.devices
    assert seen == []

    devices_interface.delete_device(base)
