"""Minimal in-container smoke harness for the device update transaction.

Runs inside python:3.12-slim with lite deps installed. Stubs the heavy
kaithem package __init__ and media bits, initializes config into a tmp
data dir, then exercises the atomic device flows directly.
"""
import gc
import json
import os
import shutil
import sys
import time
import types

DATA = "/tmp/kt_data"
shutil.rmtree(DATA, ignore_errors=True)
os.makedirs(DATA, exist_ok=True)

# Bypass kaithem/__init__.py (imports the heavy main module).
pkg = types.ModuleType("kaithem")
pkg.__path__ = ["/work/kaithem"]
sys.modules["kaithem"] = pkg

from kaithem.src import config as config_module  # noqa: E402

config_module.initialize(
    {
        "ssl_dir": f"{DATA}/ssl",
        "site_data_dir": DATA,
        "location": "0.123,0.345",
        "log_format": "normal",
        "local_access_only": True,
    }
)

from kaithem.src import devices, modules, modules_state, tagpoints  # noqa: E402

devices.drt.on_finished_loading(None)

results = []


def check(name, cond):
    results.append((name, bool(cond)))
    print(("PASS" if cond else "FAIL"), name)


events = []
from scullery import messagebus  # noqa: E402


_orig_post = devices.messagebus.post_message


def watch(prefix):
    def spy(topic, value, *a, **k):
        if topic in ("/devices/added/", "/devices/removed/") and (
            value.startswith(prefix)
        ):
            events.append((topic, value))
        return _orig_post(topic, value, *a, **k)

    devices.messagebus.post_message = spy


def settle():
    gc.collect()
    time.sleep(0.15)
    gc.collect()


def ypath(m, r):
    return f"{DATA}/modules/data/{m}/{r}.yaml"


def confdir(m, r):
    return devices.get_config_folder_from_info(
        m, r, None, create=False, always_return=True
    )


def make_parent(module, resource, name):
    data = {
        "name": name,
        "type": "DemoDevice",
        "extensions": {},
    }
    devices.makeDevice(name, data, None, module, resource)
    devices.storeDeviceInModule(data, module, resource)
    return devices.devices_host.devices[name]


# ---------------------------------------------------------------------------
# 1. Invalid JSON must change nothing
# ---------------------------------------------------------------------------
n = "mod" + str(time.time_ns())
modules.newModule(n)
base = "atom1_" + str(time.time_ns())
make_parent(n, "atomdev", base)
watch(base)

try:
    devices.updateDevice(
        base,
        {
            "temp.kaithem.store_in_module": n,
            "temp.kaithem.store_in_resource": "atomdev2",
            "json": "{bad json",
        },
    )
    check("invalid json raises", False)
except json.JSONDecodeError:
    check("invalid json raises", True)

settle()
check("invalid json: parent alive", base in devices.devices_host.devices)
check(
    "invalid json: sub alive",
    base + "/subdevice" in devices.devices_host.devices,
)
check("invalid json: old yaml", os.path.exists(ypath(n, "atomdev")))
check("invalid json: no new yaml", not os.path.exists(ypath(n, "atomdev2")))
check("invalid json: no events", events == [])

# Configure the subdevice with custom echo (subdevice in-place branch)
sub = base + "/subdevice"
subres = "atomsub"
devices.updateDevice(
    sub,
    {
        "temp.kaithem.store_in_module": n,
        "temp.kaithem.store_in_resource": subres,
        "json": json.dumps({"type": "DemoDevice", "device.echo_number": 9}),
    },
)
check(
    "subdevice echo saved",
    devices.devices_host.devices[sub].config["device.echo_number"] == 9,
)

# ---------------------------------------------------------------------------
# 2. Same-name save: no add/removed events
# ---------------------------------------------------------------------------
events.clear()
devices.updateDevice(
    base,
    {
        "temp.kaithem.store_in_module": n,
        "temp.kaithem.store_in_resource": "atomdev",
        "json": json.dumps(
            {
                "name": base,
                "type": "DemoDevice",
                "device.fixed_number_multiplier": 11,
            }
        ),
    },
)
check(
    "same-name config applied",
    devices.devices_host.devices[base].config[
        "device.fixed_number_multiplier"
    ]
    == 11,
)
check(
    "same-name keeps sub config",
    devices.devices_host.devices[sub].config["device.echo_number"] == 9,
)
check("same-name no events", events == [])

# ---------------------------------------------------------------------------
# 3. Rename: whole tree (device, subdevice, folders, resources) moves
# ---------------------------------------------------------------------------
base2 = base + "_new"
sub2 = base2 + "/subdevice"
subres2 = subres  # custom path kept

os.makedirs(confdir(n, "atomdev"), exist_ok=True)
with open(os.path.join(confdir(n, "atomdev"), "marker.txt"), "w") as f:
    f.write("x")
os.makedirs(confdir(n, subres), exist_ok=True)
with open(os.path.join(confdir(n, subres), "submarker.txt"), "w") as f:
    f.write("y")

events.clear()
devices.updateDevice(
    base,
    {
        "temp.kaithem.store_in_module": n,
        "temp.kaithem.store_in_resource": "atomdev2",
        "json": json.dumps(
            {
                "name": base2,
                "type": "DemoDevice",
                "device.fixed_number_multiplier": 7,
            }
        ),
    },
)
settle()

check("rename: old parent gone", base not in devices.devices_host.devices)
check("rename: old sub gone", sub not in devices.devices_host.devices)
check("rename: new parent", base2 in devices.devices_host.devices)
check("rename: new sub", sub2 in devices.devices_host.devices)
check(
    "rename: parent config",
    devices.devices_host.devices[base2].config[
        "device.fixed_number_multiplier"
    ]
    == 7,
)
check(
    "rename: sub config preserved",
    devices.devices_host.devices[sub2].config["device.echo_number"] == 9,
)
check(
    "rename: new parent tag",
    bool(tagpoints.allTagsAtomic[f"/devices/{base2}.random"]()),
)
check(
    "rename: old parent tag gone",
    f"/devices/{base}.random" not in tagpoints.allTags,
)
check(
    "rename: new sub tag",
    bool(tagpoints.allTagsAtomic[f"/devices/{sub2}.echo_number"]()),
)
am = modules_state.ActiveModules[n]
check("rename: resource new parent", "atomdev2" in am)
check("rename: resource new sub", subres2 in am)
check("rename: resource old parent gone", "atomdev" not in am)
check("rename: sub resource renamed in place",
      am[subres2]["device"]["name"] == sub2)
check("rename: yaml new parent", os.path.exists(ypath(n, "atomdev2")))
check("rename: yaml new sub", os.path.exists(ypath(n, subres2)))
check("rename: yaml old parent gone", not os.path.exists(ypath(n, "atomdev")))
check("rename: yaml sub kept", os.path.exists(ypath(n, subres)))
check(
    "rename: folder marker moved",
    os.path.exists(os.path.join(confdir(n, "atomdev2"), "marker.txt")),
)
check(
    "rename: subfolder marker stays",
    os.path.exists(os.path.join(confdir(n, subres2), "submarker.txt")),
)
check("rename: old folder gone", not os.path.exists(confdir(n, "atomdev")))
check("rename: subfolder stays", os.path.exists(confdir(n, subres)))
leftovers = [
    x
    for x in os.listdir(f"{DATA}/modules/data/{n}/__filedata__")
    if ".staging-" in x or ".bak-" in x or ".aside-" in x
]
check("rename: no staging leftovers", leftovers == [])
added = sorted(v for k, v in events if k.endswith("added/"))
removed = sorted(v for k, v in events if k.endswith("removed/"))
check("rename: added events", added == sorted([base2, sub2]))
check("rename: removed events", removed == sorted([base, sub]))
check(
    "rename: removed event first",
    events[0][0].endswith("removed/"),
)
check(
    "rename: cache no old parent",
    base not in devices.device_data_cache,
)
check("rename: cache no old sub", sub not in devices.device_data_cache)

# ---------------------------------------------------------------------------
# 4. Plugin init failure -> rollback
# ---------------------------------------------------------------------------
from iot_devices.devices.demo import DemoDevice  # noqa: E402

base3 = base2 + "_v3"
real_init = DemoDevice.__init__


def boom(self, data, **kw):
    if data.get("name") == base3:
        raise RuntimeError("synthetic init failure")
    return real_init(self, data, **kw)


DemoDevice.__init__ = boom
events.clear()
try:
    devices.updateDevice(
        base2,
        {
            "temp.kaithem.store_in_module": n,
            "temp.kaithem.store_in_resource": "atomdev3",
            "json": json.dumps(
                {
                    "name": base3,
                    "type": "DemoDevice",
                    "device.fixed_number_multiplier": 3,
                }
            ),
        },
    )
    check("init failure raised", False)
except RuntimeError as e:
    check("init failure raised", "synthetic init failure" in str(e))
finally:
    DemoDevice.__init__ = real_init

settle()
check("rollback: parent alive", base2 in devices.devices_host.devices)
check("rollback: sub alive", sub2 in devices.devices_host.devices)
check(
    "rollback: sub config preserved",
    devices.devices_host.devices[sub2].config["device.echo_number"] == 9,
)
check(
    "rollback: parent type real",
    devices.devices_host.devices[base2].device.device_type == "DemoDevice",
)
check("rollback: no new parent name", base3 not in devices.devices_host.devices)
check("rollback: yaml old restored", os.path.exists(ypath(n, "atomdev2")))
check("rollback: yaml new absent", not os.path.exists(ypath(n, "atomdev3")))
check(
    "rollback: folder marker restored",
    os.path.exists(os.path.join(confdir(n, "atomdev2"), "marker.txt")),
)
check(
    "rollback: new folder absent",
    not os.path.exists(confdir(n, "atomdev3")),
)
check("rollback: no events", events == [])
check(
    "rollback: cache no new name",
    base3 not in devices.device_data_cache,
)

# ---------------------------------------------------------------------------
# 5. Cross-module move
# ---------------------------------------------------------------------------
n2 = n + "_dst"
modules.newModule(n2)
# Move child to a custom resource first
devices.updateDevice(
    sub2,
    {
        "temp.kaithem.store_in_module": n,
        "temp.kaithem.store_in_resource": "customsub",
        "json": json.dumps({"type": "DemoDevice", "device.echo_number": 9}),
    },
)
events.clear()
devices.updateDevice(
    base2,
    {
        "temp.kaithem.store_in_module": n2,
        "temp.kaithem.store_in_resource": "atomdev",
        "json": json.dumps(
            {
                "name": base2,
                "type": "DemoDevice",
                "device.fixed_number_multiplier": 13,
            }
        ),
    },
)
settle()
am1 = modules_state.ActiveModules[n]
am2 = modules_state.ActiveModules[n2]
check("xmod: parent gone from old module", "atomdev2" not in am1)
check("xmod: custom sub gone old", "customsub" not in am1)
check("xmod: parent in new", "atomdev" in am2)
check(
    "xmod: custom sub keeps path in new module",
    "customsub" in am2 and am2["customsub"]["device"]["name"] == sub2,
)
check("xmod: parent config", devices.devices_host.devices[base2].config[
    "device.fixed_number_multiplier"
] == 13)
check(
    "xmod: sub config",
    devices.devices_host.devices[sub2].config["device.echo_number"] == 9,
)
check(
    "xmod: yaml parent new", os.path.exists(ypath(n2, "atomdev"))
)
check("xmod: yaml sub new", os.path.exists(ypath(n2, "customsub")))
fd1 = f"{DATA}/modules/data/{n}/__filedata__"
fd2 = f"{DATA}/modules/data/{n2}/__filedata__"
check("xmod: old parent folder gone", not os.path.exists(os.path.join(fd1, "atomdev2.config.d")))
check("xmod: old sub folder gone", not os.path.exists(os.path.join(fd1, "customsub.config.d")))
check("xmod: new parent folder", os.path.exists(os.path.join(fd2, "atomdev.config.d")))
check("xmod: new sub folder", os.path.exists(os.path.join(fd2, "customsub.config.d")))
check("xmod: parent folder content moved",
      os.path.exists(os.path.join(fd2, "atomdev.config.d", "marker.txt")))
check("xmod: same names no events", events == [])

# ---------------------------------------------------------------------------
# 6. Up-front rejections: type change, name conflict, locks
# ---------------------------------------------------------------------------
events.clear()
try:
    devices.updateDevice(
        base2,
        {
            "temp.kaithem.store_in_module": n2,
            "temp.kaithem.store_in_resource": "atomdev",
            "json": json.dumps({"name": base2, "type": "GPIODevice"}),
        },
    )
    check("type change rejected", False)
except RuntimeError as e:
    check("type change rejected", "Expected DemoDevice" in str(e))
check("type change: parent alive", base2 in devices.devices_host.devices)

other = base2 + "peer"
make_parent(n2, "otherdev", other)
try:
    devices.updateDevice(
        base2,
        {
            "temp.kaithem.store_in_module": n2,
            "temp.kaithem.store_in_resource": "atomdevx",
            "json": json.dumps({"name": other, "type": "DemoDevice"}),
        },
    )
    check("name conflict rejected", False)
except RuntimeError as e:
    check("name conflict rejected", "already exists" in str(e))
check("name conflict: both alive", other in devices.devices_host.devices)
devices.devices_host.close_device(other)
modules_state.rawDeleteResource(n2, "otherdev")

modules_state.ActiveModules[n2]["atomdev"]["resource"]["lock"] = True
try:
    devices.updateDevice(
        base2,
        {
            "temp.kaithem.store_in_module": n2,
            "temp.kaithem.store_in_resource": "atomdev",
            "json": json.dumps({"name": base2, "type": "DemoDevice"}),
        },
    )
    check("resource lock rejected", False)
except PermissionError:
    check("resource lock rejected", True)
del modules_state.ActiveModules[n2]["atomdev"]["resource"]["lock"]
check("locks: no events", events == [])

# ---------------------------------------------------------------------------
# 7. Disk write failure during commit -> rollback (same resource overwrite)
# ---------------------------------------------------------------------------
real_save = modules_state.save_resource


def boom_save(module, resource, resourceData, name=None):
    if (
        resourceData.get("device", {}).get(
            "device.fixed_number_multiplier"
        )
        == 12345
    ):
        raise OSError("synthetic disk failure")
    return real_save(module, resource, resourceData, name)


modules_state.save_resource = boom_save
events.clear()
try:
    devices.updateDevice(
        base2,
        {
            "temp.kaithem.store_in_module": n2,
            "temp.kaithem.store_in_resource": "atomdev",
            "json": json.dumps(
                {
                    "name": base2,
                    "type": "DemoDevice",
                    "device.fixed_number_multiplier": 12345,
                }
            ),
        },
    )
    check("disk failure raised", False)
except OSError as e:
    check("disk failure raised", "synthetic disk failure" in str(e))
finally:
    modules_state.save_resource = real_save
settle()
check("disk failure: parent alive", base2 in devices.devices_host.devices)
check("disk failure: sub alive", sub2 in devices.devices_host.devices)
with open(ypath(n2, "atomdev")) as f:
    check("disk failure: yaml old content", "12345" not in f.read())
check("disk failure: no events", events == [])

# ---------------------------------------------------------------------------
# 8. Saving a subdevice that had no stored resource yet (runtime defaults
#    to the dotted path) must land in the requested resource, not ghost.
# ---------------------------------------------------------------------------
n3 = "mod" + str(time.time_ns()) + "_firstsave"
modules.newModule(n3)
b3 = "firstsave_" + str(time.time_ns())
make_parent(n3, "atomdev", b3)
s3 = b3 + "/subdevice"
devices.updateDevice(
    s3,
    {
        "temp.kaithem.store_in_module": n3,
        "temp.kaithem.store_in_resource": "mysub",
        "json": json.dumps({"type": "DemoDevice", "device.echo_number": 4}),
    },
)
am3 = modules_state.ActiveModules[n3]
check("firstsave: resource created", "mysub" in am3)
check(
    "firstsave: stored name correct",
    am3["mysub"]["device"]["name"] == s3,
)
check(
    "firstsave: no dotted ghost resource",
    not any(rr.endswith(".d/subdevice") for rr in am3),
)
check(
    "firstsave: runtime config",
    devices.devices_host.devices[s3].config["device.echo_number"] == 4,
)
check("firstsave: yaml exists", os.path.exists(ypath(n3, "mysub")))

print()
fails = [n for n, ok in results if not ok]
print(f"TOTAL {len(results)} FAIL {len(fails)}")
if fails:
    print("FAILED:", fails)
    sys.exit(1)
