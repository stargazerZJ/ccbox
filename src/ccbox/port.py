"""Port forwarding between host and container via LXD proxy devices."""

from __future__ import annotations

from ccbox import lxd

DEVICE_PREFIX = "port-"


def _parse_addr_port(spec: str, default_addr: str = "127.0.0.1") -> tuple[str, int]:
    """Parse '[addr:]port' into (addr, port)."""
    if ":" in spec:
        addr, port_s = spec.rsplit(":", 1)
        return addr, int(port_s)
    return default_addr, int(spec)


def _proto(udp: bool) -> str:
    return "udp" if udp else "tcp"


def _device_name(direction: str, proto: str, port: int) -> str:
    """Generate a device name like port-fwd-tcp-8080."""
    return f"{DEVICE_PREFIX}{direction}-{proto}-{port}"


def add_forward(
    container: str,
    container_port: int,
    host_addr: str,
    host_port: int,
    udp: bool = False,
) -> str:
    """Container→Host: container's localhost:A reaches host's addr:B.

    Uses bind=instance so the listener is inside the container.
    """
    proto = _proto(udp)
    name = _device_name("fwd", proto, container_port)
    lxd.add_proxy_device(
        container,
        name,
        listen=f"{proto}:127.0.0.1:{container_port}",
        connect=f"{proto}:{host_addr}:{host_port}",
        bind="instance",
    )
    return name


def add_expose(
    container: str,
    container_port: int,
    bind_addr: str = "127.0.0.1",
    bind_port: int | None = None,
    udp: bool = False,
) -> str:
    """Host→Container: host binds addr:B, forwards to container's localhost:A.

    Uses bind=host so the listener is on the host.
    """
    if bind_port is None:
        bind_port = container_port
    proto = _proto(udp)
    name = _device_name("exp", proto, bind_port)
    lxd.add_proxy_device(
        container,
        name,
        listen=f"{proto}:{bind_addr}:{bind_port}",
        connect=f"{proto}:127.0.0.1:{container_port}",
        bind="host",
    )
    return name


def remove_port(container: str, name: str) -> None:
    """Remove a port forwarding device by name."""
    lxd.remove_device(container, name)


def list_ports(container: str) -> list[dict]:
    """List all port forwarding devices on a container."""
    devices = lxd.list_devices(container)
    result = []
    for name, props in devices.items():
        if not name.startswith(DEVICE_PREFIX):
            continue
        if props.get("type") != "proxy":
            continue
        listen = props.get("listen", "")
        connect = props.get("connect", "")
        bind = props.get("bind", "host")
        direction = "expose" if bind == "host" else "forward"
        result.append({
            "name": name,
            "direction": direction,
            "listen": listen,
            "connect": connect,
        })
    return result
