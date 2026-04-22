from enum import IntEnum

MQTT_KEEP_ALIVE: int = 10


class MsgEnum(IntEnum):
    """Enumerate application-level message types."""

    MSG = 1
    MSGACK = 2
    PROBE = 3


class MQTTEnum(IntEnum):
    """Enumerate MQTT control packet types."""

    CONNECT = 1
    CONNACK = 2
    PUBLISH = 3
    PUBACK = 4
    PUBREC = 5
    PUBREL = 6
    PUBCOMP = 7
    SUBSCRIBE = 8
    SUBACK = 9
    UNSUBSCRIBE = 10
    UNSUBACK = 11
    PINGREQ = 12
    PINGRESP = 13
    DISCONNECT = 14
