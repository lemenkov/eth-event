#!/usr/bin/python3

import re
from typing import Dict, List

import eth_abi
from eth_abi.exceptions import InsufficientDataBytes, NoEntriesFound, NonEmptyPaddingBytes
try:
    from eth_abi.exceptions import InvalidPointer
except ImportError:
    # Define a stub exception for older eth-abi versions
    InvalidPointer = type('InvalidPointer', (Exception,), {})
from eth_hash.auto import keccak
from eth_utils import to_checksum_address
from hexbytes import HexBytes


class ABIError(Exception):
    pass


class EventError(Exception):
    pass


class StructLogError(Exception):
    pass


class UnknownEvent(Exception):
    pass


ADD_LOG_ENTRIES = ["logIndex", "blockNumber", "transactionIndex"]


def get_log_topic(event_abi: Dict) -> str:
    """
    Generate an encoded event topic for an event.

    Arguments
    ---------
    event_abi : Dict
        Dictionary from a contract ABI, describing a specific event.

    Returns
    -------
    str
        bytes32 encoded topic for the event.
    """
    if not isinstance(event_abi, dict):
        raise TypeError("Must be a dictionary of the specific event's ABI")
    if event_abi.get("anonymous"):
        raise ABIError("Anonymous events do not have a topic")

    types = _params(event_abi["inputs"])
    key = f"{event_abi['name']}({','.join(types)})".encode()

    return _0xstring(keccak(key))


def get_topic_map(abi: List) -> Dict:
    """
    Generate a dictionary of event topics from an ABI.

    This dictionary is required by `decode_log`, `decode_logs`, and
    `decode_traceTransaction`.

    Anonymous events are ignored. The return data is formatted as follows:

        {
            'encoded bytes32 topic': {
                'name':"Event Name",
                'inputs': [abi inputs]
            }
        }

    Arguments
    ---------
    abi : List
        Contract ABI

    Returns
    -------
    Dict
        Mapping of contract events.
    """
    try:
        events = [i for i in abi if i["type"] == "event" and not i.get("anonymous")]
        return {get_log_topic(i): {"name": i["name"], "inputs": i["inputs"]} for i in events}

    except (KeyError, TypeError):
        raise ABIError("Invalid ABI")


def decode_log(log: Dict, topic_map: Dict) -> Dict:
    """
    Decode a single event log from a transaction receipt.

    Indexed arrays cannot be decoded. The returned value will still
    be encoded. Anonymous events and events where the topic is not found in
    `topic_map` will raise an exception.

    The return data is formatted as follows:

    {
        'name': "",  # event name
        'address': "",  # address where the event was emitted
        'decoded': True / False,
        'data': [{
            'name': "",  # variable name
            'type': "",  # type as given by the ABI
            'value': "",  # decoded value, formatted by `eth_abi.decode_single`
            'decoded': True / False
        }, ...]
    }

    And additional entries: 'logIndex', 'blockNumber', 'transactionIndex', if
    they are present in log.

    Arguments
    ---------
    log : Dict
        Event log as returned from the `eth_getTransactionReceipt` RPC endpoint.
    topic_map : Dict
        Contract event map generated by `get_topic_map`

    Returns
    -------
    Dict
        Decoded event log.
    """
    if not log["topics"]:
        raise EventError("Cannot decode an anonymous event")

    key = _0xstring(log["topics"][0])
    if key not in topic_map:
        raise UnknownEvent("Event topic is not present in given ABI")
    abi = topic_map[key]

    try:
        event = {
            "name": abi["name"],
            "data": _decode(abi["inputs"], log["topics"][1:], log["data"]),
            "decoded": True,
            "address": to_checksum_address(log["address"]),
        }
        event = append_additional_log_data(log, event, ADD_LOG_ENTRIES)
        return event
    except (KeyError, TypeError):
        raise EventError("Invalid event")


def decode_logs(logs: List, topic_map: Dict, allow_undecoded: bool = False) -> List:
    """
    Decode a list of event logs from a transaction receipt.

    If `allow_undecoded` is `True`, an undecoded event is returned with the
    following structure:

    {
        'name': None,
        'decoded': False,
        'data': "",  # raw data hexstring
        'topics': [],  # list of undecoded topics as 32 byte hexstrings
        'address: "",  # address where the event was emitted
    }

    And additional entries: 'logIndex', 'blockNumber', 'transactionIndex', if
    they are present in log.

    Arguments
    ---------
    logs : List
        List of event logs as returned from the `eth_getTransactionReceipt`
        RPC endpoint.
    topic_map : Dict
        Contract event map generated by `get_topic_map`
    allow_undecoded: bool, optional
        Determines how undecodable events are handled. If `True`, they are
        returned

    Returns
    -------
    List
        A list of decoded events, formatted in the same structure as `decode_log`
    """
    events = []

    for item in logs:
        topics = [_0xstring(i) for i in item["topics"]]
        if not topics or topics[0] not in topic_map:
            if not allow_undecoded:
                raise UnknownEvent("Log contains undecodable event")
            event = {
                "name": None,
                "topics": topics,
                "data": _0xstring(item["data"]),
                "decoded": False,
                "address": to_checksum_address(item["address"]),
            }
            event = append_additional_log_data(item, event, ADD_LOG_ENTRIES)
        else:
            event = decode_log(item, topic_map)

        events.append(event)

    return events


def append_additional_log_data(log: Dict, event: Dict, log_entries: List[str]):
    for log_entry in log_entries:
        if log_entry in log:
            event[log_entry] = log[log_entry]
    return event


def decode_traceTransaction(
    struct_logs: List, topic_map: Dict, allow_undecoded: bool = False, initial_address: str = None
) -> List:
    """
    Extract and decode a list of event logs from a transaction traceback.

    Useful for obtaining the events fired in a transaction that reverted.

    Arguments
    ---------
    struct_logs : List
        `structLogs` field from Geth's `debug_traceTransaction` RPC endpoint
    topic_map : Dict
        Contract event map generated by `get_topic_map`
    allow_undecoded : bool, optional
        If `False`, an exception is raised when an event cannod be decoded.
    initial_address : str, optional
        The initial address being called in the trace. If given, the decoded
        events will also include addresses.

    Returns
    -------
    List
        A list of decoded events, formatted in the same structure as `decode_log`
    """
    events = []
    if initial_address is not None:
        address_list: List = [to_checksum_address(initial_address)]
    else:
        address_list = [None]

    last_step = struct_logs[0]
    for i in range(1, len(struct_logs)):
        step = struct_logs[i]
        if initial_address is not None:
            if step["depth"] > last_step["depth"]:
                if last_step["op"] in ("CREATE", "CREATE2"):
                    out_step = next(x for x in struct_logs[i:] if x["depth"] == last_step["depth"])
                    address = to_checksum_address(f"0x{out_step['stack'][-1][-40:]}")
                    address_list.append(address)
                else:
                    address = to_checksum_address(f"0x{last_step['stack'][-2][-40:]}")
                    address_list.append(address)

            elif step["depth"] < last_step["depth"]:
                address_list.pop()

        last_step = step
        if not step["op"].startswith("LOG"):
            continue

        try:
            offset = int(step["stack"][-1], 16)
            length = int(step["stack"][-2], 16)
            topic_len = int(step["op"][-1])
            topics = [_0xstring(i) for i in step["stack"][-3 : -3 - topic_len : -1]]
        except KeyError:
            raise StructLogError("StructLog has no stack")
        except (IndexError, TypeError):
            raise StructLogError("Malformed stack")

        try:
            data = _0xstring(HexBytes("".join(step["memory"]))[offset : offset + length].hex())
        except (KeyError, TypeError):
            raise StructLogError("Malformed memory")

        if not topics or topics[0] not in topic_map:
            if not allow_undecoded:
                raise UnknownEvent("Log contains undecodable event")
            result = {
                "name": None,
                "topics": topics,
                "data": data,
                "decoded": False,
                "address": address_list[-1],
            }
        else:
            result = {
                "name": topic_map[topics[0]]["name"],
                "data": _decode(topic_map[topics[0]]["inputs"], topics[1:], data),
                "decoded": True,
                "address": address_list[-1],
            }
        events.append(result)

    return events


def _0xstring(value: bytes) -> str:
    # placeholder, will be used to prepend bytes with 0x to avoid HexBytes v1 breaking change
    return f"{HexBytes(value).hex()}"


def _params(abi_params: List) -> List:
    types = []
    # regex with 2 capturing groups
    # first group captures whether this is an array tuple
    # second group captures the size if this is a fixed size tuple
    pattern = re.compile(r"tuple(\[(\d*)\])?")
    for i in abi_params:
        tuple_match = pattern.match(i["type"])
        if tuple_match:
            _array, _size = tuple_match.group(1, 2)  # unpack the captured info
            tuple_type_tail = f"[{_size}]" if _array is not None else ""
            types.append(f"({','.join(x for x in _params(i['components']))}){tuple_type_tail}")
            continue
        types.append(i["type"])

    return types


def _decode(inputs: List, topics: List, data: str) -> List:
    indexed_count = len([i for i in inputs if i["indexed"]])

    if indexed_count and not topics:
        # special case - if the ABI has indexed values but the log does not,
        # we should still be able to decode the data
        unindexed_types = inputs

    else:
        if indexed_count < len(topics):
            raise EventError(
                "Event log does not contain enough topics for the given ABI - this"
                " is usually because an event argument is not marked as indexed"
            )
        if indexed_count > len(topics):
            raise EventError(
                "Event log contains more topics than expected for the given ABI - this is"
                " usually because an event argument is incorrectly marked as indexed"
            )
        unindexed_types = [i for i in inputs if not i["indexed"]]

    # decode the unindexed event data
    try:
        unindexed_types = _params(unindexed_types)
    except (KeyError, TypeError):
        raise ABIError("Invalid ABI")

    if unindexed_types and data == "0x":
        length = len(unindexed_types) * 32
        data = _0xstring(length)

    try:
        decoded = list(eth_abi.decode(unindexed_types, HexBytes(data)))[::-1]
    except InsufficientDataBytes:
        raise EventError("Event data has insufficient length")
    except NonEmptyPaddingBytes:
        raise EventError("Malformed data field in event log")
    except InvalidPointer as e:
        raise EventError(str(e))
    except OverflowError:
        raise EventError("Cannot decode event due to overflow error")

    # decode the indexed event data and create the returned dict
    topics = topics[::-1]
    result = []
    for i in inputs:
        result.append({"name": i["name"], "type": i["type"]})

        if "components" in i:
            result[-1]["components"] = i["components"]

        if topics and i["indexed"]:
            encoded = HexBytes(topics.pop())
            try:
                value = eth_abi.decode([i["type"]], encoded)[0]
            except (InsufficientDataBytes, NoEntriesFound, OverflowError):
                # an array or other data type that uses multiple slots
                result[-1].update({"value": _0xstring(encoded), "decoded": False})
                continue
        else:
            value = decoded.pop()

        if isinstance(value, bytes):
            value = _0xstring(value)
        result[-1].update({"value": value, "decoded": True})

    return result
