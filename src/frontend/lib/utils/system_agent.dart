/// The built-in system agent user, as seen by user-targeting UIs (#2892).
///
/// The agent realizes its capabilities through in-container physical
/// access, never as an authenticated principal. The backend therefore
/// rejects — with `AgentPrincipalError` — deleting it, changing its
/// email/handle/password, disabling it, or making it an ACL principal or
/// group member. Surfaces that offer such actions use [isSystemAgent] to
/// omit them for the agent instead of offering them and surfacing the
/// rejection.

/// The agent's fixed, published id (`AGENT_USER_ID` in the backend model).
const String agentUserId = '00000000-0000-0000-0000-000000000001';

/// True when [user] — a row from a user-listing endpoint — is the system
/// agent.
///
/// Matched by id alone: it is the invariant every backend guard and DB
/// trigger keys on, and it is a UUID primary key, so no other row can
/// hold it. `users.provider` is deliberately NOT a marker — an OIDC
/// provider id is stored verbatim in that column, so a provider named
/// `system` would flag its human users as the agent and strip their
/// edit/delete affordances.
bool isSystemAgent(Map<String, dynamic> user) => user['id'] == agentUserId;
