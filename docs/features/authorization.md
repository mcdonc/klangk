# Authorization

- **ACL authorization**: Pyramid-style ACL system with resource tree, principals (user/group/system), and ordered allow/deny ACEs. See [ACL System](../reference/acl.md) for details.
- Default user auto-seeded on startup in the `admin` group (configurable via `KLANGK_DEFAULT_USER/PASSWORD` in `.env`)
