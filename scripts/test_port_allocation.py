"""Test port allocation lifecycle: create, increase, decrease, delete."""

import asyncio
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from backend import user_store, container_manager


async def main():
    await user_store.init_db()

    workspace_id = "test-port-workspace"
    user_id = "test-port-user"

    # Clean up from previous runs
    await user_store.delete_workspace(workspace_id, user_id)

    # Ensure test user exists
    existing = await user_store.get_user_by_username("test-port-user")
    if existing is None:
        import bcrypt

        pw = bcrypt.hashpw(b"test", bcrypt.gensalt()).decode()
        await user_store.create_user("test-port-user", pw)
        existing = await user_store.get_user_by_username("test-port-user")
    user_id = existing["id"]

    # Create workspace with default 5 ports
    print("=== Test 1: Workspace creation allocates ports ===")
    workspace = await user_store.create_workspace(user_id, "port-test")
    workspace_id = workspace["id"]
    await container_manager.allocate_ports(workspace_id, 5)
    ports = await user_store.get_workspace_ports(workspace_id)
    print(f"  Allocated ports: {ports}")
    assert len(ports) == 5, f"Expected 5 ports, got {len(ports)}"
    print("  PASS")

    # Increase to 8 ports
    print("\n=== Test 2: Increasing num_ports allocates more ===")
    existing_ports = await container_manager.get_workspace_ports(workspace_id)
    if len(existing_ports) < 8:
        new_ports = await container_manager.allocate_ports(
            workspace_id, 8 - len(existing_ports)
        )
        existing_ports.extend(new_ports)
    ports = await user_store.get_workspace_ports(workspace_id)
    print(f"  Ports after increase: {ports}")
    assert len(ports) == 8, f"Expected 8 ports, got {len(ports)}"
    print("  PASS")

    # Decrease to 3 ports
    print("\n=== Test 3: Decreasing num_ports deallocates excess ===")
    ports = await user_store.get_workspace_ports(workspace_id)
    if len(ports) > 3:
        excess = ports[3:]
        await user_store.remove_port_allocations(workspace_id, excess)
    ports = await user_store.get_workspace_ports(workspace_id)
    print(f"  Ports after decrease: {ports}")
    assert len(ports) == 3, f"Expected 3 ports, got {len(ports)}"
    # Verify the released ports are actually free
    all_used = await user_store.get_all_allocated_ports()
    for p in excess:
        assert p not in all_used, f"Port {p} should be free but is still allocated"
    print("  Released ports are free: PASS")
    print("  PASS")

    # Delete workspace and verify ports are freed
    print("\n=== Test 4: Workspace deletion frees all ports ===")
    ports_before_delete = await user_store.get_workspace_ports(workspace_id)
    print(f"  Ports before delete: {ports_before_delete}")
    await user_store.delete_workspace(workspace_id, user_id)
    ports_after = await user_store.get_workspace_ports(workspace_id)
    print(f"  Ports after delete: {ports_after}")
    assert len(ports_after) == 0, f"Expected 0 ports, got {len(ports_after)}"
    all_used = await user_store.get_all_allocated_ports()
    for p in ports_before_delete:
        assert p not in all_used, f"Port {p} should be free after workspace delete"
    print("  All ports freed: PASS")
    print("  PASS")

    print("\n=== All tests passed ===")


if __name__ == "__main__":
    asyncio.run(main())
