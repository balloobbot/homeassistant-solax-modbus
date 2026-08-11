# Migrating homeassistant-solax-modbus to modbus-connection

A record of what this integration does, what the migration to
[modbus-connection](https://github.com/home-assistant-libs/modbus-connection) 4.4.0
actually required, and where the library got in the way.

**Scope of the change.** The transport stack, the register codecs and the error
vocabulary moved to the library. The block planner, the entity-description
register map and the polling machinery did not — see
[question 3](#3-what-could-modbus-connection-do-better) for why the declarative
model framework cannot express them.

**What is verified.** 508 unit tests, mypy `strict`, ruff. The decoders were
compared bit-for-bit against `pymodbus.convert_from/to_registers` before the
swap, and the poll, write and quarantine paths run against the library's
in-memory backend. There is no hardware in this loop: the ASCII-over-TCP and
serial links are verified as far as "the right backend is chosen and the client
constructs", not as far as bytes on a wire.

**A correction to the brief.** The survey flagged three friction points. Two are
resolved and one holds, but not quite for the stated reason:

| Friction | Where it really lives | Outcome |
| --- | --- | --- |
| ASCII framing over TCP | `__init__.py:563-564` (before): `FramerType.ASCII` on a TCP client | Supported — `ModbusTcpParams(framer="ascii")`, **pymodbus backend only** |
| Forced FC16 for one register | `WRITE_MULTISINGLE_MODBUS`, `async_write_registers_single` | Supported — at the raw level it is just `unit.write_registers(addr, [v])`; the model's `force_fc16` flag covers the declarative path |
| Callable/dict `scale` | `const.py:244` (the survey's "line 112" is stale) | Stays custom, as predicted — but the *dict* case would fit `NumberField(convert=...)`; only the callable is genuinely unmappable |

PyPI's latest is 4.4.0, not the 4.1.0 in the brief; this targets 4.4.0.

---

## 1. What weird things does this library do?

### The register map is whatever the user enabled

There is no device model. The register map *is* the list of Home Assistant
entity descriptions, and `should_register_be_loaded()` asks the **entity
registry** whether each one is enabled. Disable a sensor in the HA UI and its
register drops out of the next block plan. `_is_dependency_for_enabled_control()`
then forces a disabled sensor *back in* when a writable control needs it as a
data source. So the block layout is a function of user preferences, changes at
runtime, and `blocks_changed` triggers a re-plan whenever an entity is added or
removed.

This is genuinely clever — you do not pay for registers you do not look at, on
devices with 800+ of them — and it is the deepest reason the declarative
`Component` model does not fit: the field set is not known at class-definition
time.

### Three scan groups, three independent block plans

Sensors are bucketed into slow/medium/fast intervals (`SCAN_GROUP_*`), each with
its own timer, its own plan and its own catch-up logic when a poll overruns its
interval. `SCAN_GROUP_AUTO` picks a bucket from the sensor's *unit of
measurement* — energy, frequency and temperature are slow, everything else is
fast.

### `ignore_readerror` is a tri-state, and only the first entity in a block votes

`False` invalidates the data, `True` tolerates the failure, and **any other
value** is a static fallback written into the data dict. Whether the whole block
failure is tolerated is decided by the `ignore_readerror` of the *first*
descriptor in the block only; the rest just supply their own fallbacks. Plugins
can also set `auto_block_ignore_readerror` to have this stamped onto every
automatically-created block.

The point is that a partially-unreadable register map is *normal* here: firmware
variants, optional battery packs, regional models. A block failing is data, not
an error.

### Runtime bad-register quarantine

Three block failures in ten minutes trigger a binary search over the block's
entity bases (`_find_bad_regs_in_block`), each candidate is confirmed with two
solo reads a second apart, and confirmed-bad addresses go into `bad_regs`, which
`splitInBlocks` then splits around. A background loop re-probes them every five
minutes with a shortened timeout and un-quarantines any that recover. There are
diagnostic sensors reporting the health, the success rate and the quarantine
list.

I have not seen another Modbus library in this survey set that self-heals its own
register map at runtime.

### Sleep mode is a first-class state, not a failure

An inverter that stops answering at night is not an error. Plugins implement
`isAwake(datadict)`; each sensor declares a `sleepmode` (zero it, clear it, or
keep the last value); polling drops to 1-in-10 cycles; failed writes are queued
in `writequeue` and replayed when the device wakes; and there is a "wakeup
button" — a register the hub writes to poke the inverter awake. Some commands are
accepted while asleep, so a write is *attempted* first and only queued if it
fails, and then queued **again** even on success so it repeats after wake-up.

### Autorepeat

A button or select can register an expiry timestamp in `hub.data["_repeatUntil"]`.
Every poll cycle re-issues that entity's write until the timer expires, then
calls its value function one final time with `BUTTONREPEAT_POST`. This is how
"charge at N amps for the next 10 minutes" is implemented against inverters whose
command registers are watchdogged.

### Local entities that never touch Modbus

`WRITE_DATA_LOCAL` entities live only in `hub.data`, are persisted to
`<hub name>_data.json` in the HA config directory, and are reloaded at startup.
They exist so a plugin can compose several user inputs into one multi-register
write — the value the user picks in a `select` is stored locally and only later
encoded into a burst.

### Writes are declared as a list of (entity key, value) tuples

`async_write_registers_multi(payload=[("charge_start_hours", 7), ("_uint16", 30)])`
— each item is either an entity key (whose `scale`, `reverse_option_dict` and
`register_data_type` are used to encode it) or a raw `REGISTER_*` type token. The
whole payload is encoded before a single byte is sent, so a bad item cannot leave
half a command written. RTC sync is a value function that *returns* such a list.

### Two entities sharing one 16-bit register

`REGISTER_U8L`/`REGISTER_U8H` are the low and high bytes of one register, stored
in the block map as a `dict` of descriptors at a single address. The block reader
decodes the word once and passes it to each entity with `advance=False` so the
cursor does not move twice.

### `REGISTER_ULSB16MSB16` is uint32

The `const.py` comment says "probably same as REGISTER_U32 - suggest to remove
later". Having read the decode both ways: it is. `big` gives
`regs[1] + regs[0] * 65536`, which is a plain big-word-order uint32; `little`
gives the little-word-order one. The migration implements it as such, with the
equivalence recorded in a comment rather than silently collapsing the type.

### Serial numbers are byte-swapped by hand

Six plugins carry the same block: decode the string, and if it does not start
with the expected letter, swap every pair of bytes. The comments blame "a bug in
`Endian.LITTLE`". It is not a bug — these devices really do ship the bytes
swapped — and it is a good example of why the review's "byte order is the
consumer's problem" call is right: the fix belongs where the device quirk is
known.

### It can borrow someone else's connection

Besides its own TCP and serial links, the integration can delegate to Home
Assistant's built-in `modbus` integration via `get_hub(hass, name).async_pb_call()`.
That transport survives this migration untouched — it is a different animal, and
the library does not own it.

### Scale is three things at once

```python
scale: float | dict | Callable[[initval, descr, datadict], Any]
```

A float multiplies. A dict maps a raw code to a display string (missing keys
become `"Unknown"`). A callable receives the raw value, the descriptor **and the
entire poll snapshot** — that last one is what makes it unmappable onto a
field-level converter. On top of it sit `read_scale`, `read_scale_exceptions`
(per-serial-number overrides applied only once local data has loaded) and
`rounding`.

### And the scale of the thing

58,000 lines across 17 plugin modules; `plugin_solax.py` alone is 12,000. Plugins
are Python *modules*, discovered by name and imported dynamically, and their
`plugin_instance` is deep-copied per hub because it carries mutable runtime state
(Sofar's discovered battery-pack serials, for one).

---

## 2. What internals of modbus-connection did you have to touch?

Nothing. Nothing was monkeypatched, and as of 4.4.0 nothing private is imported
either.

### Declared locally, as the library now intends: `ModbusParams`

The hub stores "the parameters this entry uses" so it can release the shared
link later, and typing that attribute needs a name for the union. In 4.3.0
`_client.py` defined and `__all__`-exported `ModbusParams` but
`modbus_connection/__init__.py` did not re-export it, so the choice was to
import from a private module or to redeclare. I redeclared, narrowed to what
SolaX actually opens:

```python
# modbus_link.py
ModbusParams = ModbusTcpParams | ModbusSerialParams
```

4.4.0 settled the question the other way: the alias is **deleted**, and the
public constructors spell the union inline. So this is no longer a workaround
awaiting a one-line fix — a consumer that wants to name the set of transports
*it* supports is expected to declare it, and a narrower union is more honest
here than the library's four-way one would have been. Anything that had imported
`ModbusParams` from `modbus_connection._client` would have broken on 4.4.0; this
did not.

### Subclassed as intended: `RegisterField`

Two subclasses in `fields.py` cover what the library does not ship:
`WordOrderedStringField` and `WordsField` (a raw list of words a plugin's value
function picks apart). Only `decode` is abstract, `encode` has a raising
default, and the constructor takes `count` — all public, and the seam works
exactly as the review claimed it would.

There were four. `REGISTER_U8L`/`REGISTER_U8H` — the two byte halves sharing one
16-bit register — were hand-rolled `LowByteField`/`HighByteField` doing their
own mask and shift; 4.4.0's `bits()` is exactly that field, so they are now
`bits(0, 0, 8)` and `bits(0, 8, 8)`. The existing decode tests
(`0xAB12 → 0x12 / 0xAB`) pinned the swap.

**But** I use fields *outside* a `Component`: constructed unbound at address 0
and called purely as codecs, cached per `(type, width, word order)`. The hub owns
block planning and hands the already-read words in. That works perfectly and is
the highest-value part of this migration, but nothing in the library says it is a
supported way to use a field.

### Imported by module path: `decode` / `encode`

`decode_string` is used by 17 plugins and the string field; `combine_words`,
`encode_string` and the int/float codecs by the numeric fields. None of them are
re-exported from the package root, so every import is
`from modbus_connection.decode import decode_string`. They are public modules —
just not first-class.

### Used as documented: mock, exceptions, params, `for_unit`

`MockModbusConnection`/`MockModbusUnit` replaced hand-rolled response fakes in
five test modules. The exception hierarchy is used as a neutral vocabulary even
by `CoreModbusTransport`, which the library does not own — a Core hub answering
`None` becomes a `ModbusError`, a disconnected one a `ModbusConnectionError` — so
the hub's retry and quarantine logic keys off one set of types regardless of
transport.

### Abused a value hook as a failure hook (in tests)

`MockModbusUnit.fail_read()` is *permanent*. Testing the transport's single retry
needs "fail once, then succeed", which the mock has no way to say. I smuggled the
failure through a register-value callable:

```python
def drops_the_first_frame() -> int:
    attempts.append(len(attempts) + 1)
    if len(attempts) == 1:
        raise ModbusTimeoutError("silence")
    return 42

unit.holding[9] = drops_the_first_frame
```

That is not private access, but it is not what `RegisterSpec` callables are for.

### Not adopted

`Component`, `ComponentGroup`, `ManualComponent`, `repeating_group`,
`register_ranges`, `scale_register`/SunSpec fields, coils and discrete inputs
(SolaX uses none), `message_spacing` and `connect_delay` (no device here needs
them). The first four are covered below; the rest are simply not this device's
problem.

---

## 3. What could modbus-connection do better?

Ordered by how much they cost me.

### 3.1 `ReadPlan.execute` aborts the entire plan on the first refused block

This is the single reason the model framework cannot carry this integration.

```python
# _planning.py
for start, count in space_blocks:
    try:
        got = await read(start, count)
    except ModbusExceptionError as err:
        raise ModbusExceptionError.from_code(...)   # every later block is never read
```

A SolaX device group is 10–40 blocks and a block failing is routine. This
integration reads **every** block, decides tolerance **per block** from that
block's first descriptor, writes per-descriptor fallback values for the ones that
failed, and reports `SUCCESS` / `PARTIAL` / `FAILED` from the mix. A timeout is
not even caught in `execute` — it propagates straight out, killing the rest of
the read.

`ManualComponent` is otherwise an excellent fit: runtime `add`/`remove` matches
the enable/disable-driven map, plan invalidation matches quarantine, and two
fields at one address matches the byte halves. It falls over on exactly this one
behaviour.

**Concretely:** let `execute` continue and report. Either

```python
results = await plan.execute(unit, on_block_error="collect")   # -> {ReadBlock: ModbusError | None}
```

or a `_verify_read`-style hook invoked per failed block that can say
*abort* / *tolerate*. Without it, any device with a partially-unreadable map has
to reimplement planning and reading to get one policy decision back, which means
giving up the whole `Component` layer.

### 3.2 The two backends are not interchangeable, and the capability matrix is undocumented

The README says "two interchangeable backends". For this consumer they are not,
in both directions:

- **ASCII-over-TCP is pymodbus-only.** `tmodbus/__init__.py:88` raises at
  construction.
- **serialx URLs are tmodbus-only.** This integration's config flow accepts
  `esphome-hass://esphome/<entry>?port_name=...` for remote RS485 adapters.
  tmodbus's serial transport is serialx and opens them; pymodbus is pyserial and
  cannot.

So the integration ships **both** extras and hardcodes the choice:

```python
def _connection_class(params):
    if isinstance(params, ModbusTcpParams) and params.framer == "ascii":
        return PymodbusConnection
    return TmodbusConnection
```

I learned both facts by reading backend source, not docs. **Concretely:** either
a `modbus_connection.connect(params)` factory in the top-level package that picks
a backend able to serve the params (and raises a useful error when neither can),
or at minimum a capability table in the docs. A device library that needs both
backends is not an exotic case — it is any library that supports serial *and* a
legacy ASCII gateway.

### 3.3 Retries are delegated, silently

Both backends are constructed with retries disabled (`retries=0`,
`retry_never`), which is a defensible policy call — but nothing says so, and
every consumer then writes the same loop. Mine:

```python
REQUEST_RETRIES = 1

async def _retried(self, operation, call):
    for attempt in range(REQUEST_RETRIES + 1):
        try:
            return await call()
        except (ModbusTimeoutError, ModbusConnectionError) as err:
            if attempt >= REQUEST_RETRIES:
                raise
```

The foxess migration wrote the same thing. **Concretely:** either a
`retry_policy=` on the connection (attempt count plus which exception types), or
one paragraph in the README saying retries are deliberately the caller's job.
The current silence reads like they are handled.

### 3.4 `StringField` has no `word_order`

Every numeric field takes `word_order`. `StringField` does not — and pymodbus's
`convert_from_registers(..., DATATYPE.STRING, word_order=...)` **does** apply it.
Six SolaX plugins declare `order32="little"` *and* have string registers, so
migrating naively would have silently changed how their serial numbers and
firmware strings decode. I had to write:

```python
class WordOrderedStringField(RegisterField[str]):
    def decode(self, words, scale_exponent=None):
        return decode_string(words if self.word_order == "big" else list(reversed(words)))
```

**Concretely:** add `word_order` to `string()`/`StringField`, or state in the
docs that strings are deliberately word-order-agnostic so a migrating consumer
knows to check. Silently differing from pymodbus on a type that carries device
identity is the worst of the three options.

### 3.5 The mock cannot express a transient failure

`fail_read`, `fail_write` and `fail_requests` are all permanent until cleared.
The library delegates retries to consumers (3.3) and then gives them no supported
way to test one. **Concretely:** `fail_read(addr, err, times=1)`, or a
`fail_next(n, err)`. Two lines in `_raise_if_read_fails`.

### 3.6 `for_unit()` caches in the mock and not in the backends

```python
MockModbusConnection.for_unit  -> cached, returns the same object
TmodbusConnection.for_unit     -> new TmodbusUnit(self, unit_id) every call
PymodbusConnection.for_unit    -> new PymodbusUnit(self, unit_id) every call
```

Handles are stateless (per-unit `message_spacing` lives on the connection's
pacer), so this is not a correctness bug — but a test asserting handle identity
passes on the mock and fails in production. I cache handles in the transport
anyway. **Concretely:** cache in the backends too, or drop it from the mock.

### 3.7 Error messages name the operation but not the unit

`_describe` renders `read_holding_registers(9, 2)`. With connection sharing —
the library's own thesis — one link serves many devices, and the first thing you
want from a log line is *which* one failed. **Concretely:** put the unit id in
the prefix: `unit 3: read_holding_registers(9, 2)`.

### 3.8 Bless `RegisterField` as a standalone codec

Constructing a field unbound and calling `decode(words)` / `encode(value)` was
the cleanest part of this migration — it turned a 50-line `if/elif` chain over
pymodbus datatype enums into a cached lookup, and put the device's exotic types
behind a documented extension point. But the docs frame fields purely as
`Component` attributes. Say that `decode`/`encode` are a supported public codec
API for consumers that own their own planning; it is the natural on-ramp for
exactly the libraries that cannot use `Component` (see 3.1).

### 3.9 Smaller things

- **Re-export the codecs.** `from modbus_connection import decode_string` should
  work; `modbus_connection.decode` is public but second-class.
- **`disconnect()` on a shared connection is a shared decision.** The docstring
  describes recycling "a peer that keeps the socket open but stops answering" —
  correct, and benign here since everyone reconnects on demand, but with sharing
  as the headline feature it should say out loud that one consumer's
  `disconnect()` drops the link for every other consumer on that endpoint.
- **`ReadBlock` is a model concept in the top-level exceptions module.**
  `ModbusExceptionError.block` is only ever populated by `ReadPlan.execute`, so a
  consumer doing raw block reads always sees `None` and tracks the block itself.
  Fine, but it makes the top-level exception look like it carries more than it
  does.

---

## What the library made better here

Worth saying plainly, because the list above is all complaints.

- **Connect-on-demand plus internal serialization deleted real code**: a
  per-request `asyncio.Lock`, a `_check_connection()` before every request, and
  the reconnect dance around them. The lock in particular was load-bearing
  scaffolding around a sync-ish client.
- **The typed hierarchy bought a behavioural improvement, not just tidiness.** An
  `IllegalDataAddressError` means the device positively denies the address, so
  quarantine now starts immediately instead of after three failures over ten
  minutes. And "the device answered with an exception" versus "the device said
  nothing" is now a type distinction rather than a `response.isError()` guess,
  which is exactly what `communication_succeeded` needed.
- **Sharing is the right feature for this integration.** It already *detected*
  multiple entries on one endpoint and could only warn. Now inverters behind one
  RS485 gateway share one internally-serialized connection and select themselves
  with `for_unit()`. `ModbusParams.endpoint` deliberately ignoring framer and
  line settings is exactly the right key.
- **`pymodbus_compat.py` is gone.** 321 lines of version sniffing across pymodbus
  3.8–3.11, three fallback import paths for `convert_to_registers`, a shadow
  `DataType` enum aliased onto whichever real one was found, and a
  `BinaryPayloadBuilder` legacy path. All of it replaced by pinning
  modbus-connection, which pins a pymodbus floor.
- **The decoders are bit-identical.** Every type this integration uses
  round-trips to the same words as `convert_from/to_registers`, checked before
  the swap. That made a 17-plugin change a mechanical one rather than a risky
  one.
- **The mock backend is good.** Five test modules dropped their hand-rolled
  response fakes, and `WriteEvent.function_code` let the FC06-vs-FC16 tests
  assert the thing they actually care about instead of which method was called.
