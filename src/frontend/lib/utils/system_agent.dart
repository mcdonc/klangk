/// The built-in system agent user, as seen by admin UIs (#2892).
///
/// The agent (email `klangk@example.com`, handle `klangk`) realizes its
/// capabilities through in-container physical access, never as an
/// authenticated principal. The backend therefore rejects — with
/// `AgentPrincipalError` — deleting it, changing its email/handle/password,
/// disabling it, or making it an ACL principal or group member. Admin
/// surfaces use [isSystemAgent] to mark the row and omit those actions
/// instead of offering them and surfacing the rejection.

/// The agent's fixed, published id (`AGENT_USER_ID` in the backend model).
const String agentUserId = '00000000-0000-0000-0000-000000000001';

/// True when [user] — a row from `GET /admin/users` — is the system agent.
///
/// The seeded agent row carries `provider == 'system'`, a value unique to
/// it (human/OIDC users are `local` or an OIDC provider slug) that a DB
/// trigger keeps immutable; the id check pins the published constant too,
/// so either marker identifies the row.
bool isSystemAgent(Map<String, dynamic> user) =>
    user['id'] == agentUserId || user['provider'] == 'system';
