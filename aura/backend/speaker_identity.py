"""Who is talking to Akansha, and how much authority that answer carries.

This module exists because of a measured defect, not a hypothetical one. The chat
endpoint accepts a `speaker_profile` in the request body and resolved it like this::

    merged = {**_default_owner_speaker_profile(db), **req.speaker_profile}
    if not merged.get("access_level"):
        merged["access_level"] = _speaker_access_level(...)

The guard never fires. `_default_owner_speaker_profile` already supplies
``access_level: "owner"``, so ``merged.get("access_level")`` is always truthy and
the derived value is never used. Every claimed speaker came back as the owner --
including one that explicitly said it was a guest. Measured against the running
app before this module existed:

    relationship 'friend' -> access=owner   closeness=close
    relationship None     -> access=owner   closeness=close
    relationship 'guest'  -> access=owner   closeness=close
    relationship 'owner'  -> access=owner   closeness=close

The same merge leaked the owner's personal context. `notes` carries their bio,
`context_profile` carries their education and current project, and
`conversation_summary` carries what they have been working on -- so a request
claiming to be a visitor received all of it as *its own* profile and the model
was handed it as background for that visitor.

What this module does NOT do, stated plainly because the alternative is security
theatre: it performs no biometric verification. There is no voiceprint, no speaker
embedding, no face recognition, and no acoustic model anywhere in this codebase.
Identification here is *self-declaration* -- the caller says who they are and this
module decides how much that claim is worth. That is a real improvement over
granting owner authority to every claim, and it is not authentication. The
`identification_method` field on every resolved profile records which of the two
it was, so nothing downstream can mistake one for the other.
"""

from __future__ import annotations

from typing import Any


# Access levels, weakest to strongest. Stored on `speaker_profiles.access_level`.
GUEST = "guest"
TRUSTED = "trusted"
OWNER = "owner"

ACCESS_LEVELS = (GUEST, TRUSTED, OWNER)
_LEVEL_RANK = {GUEST: 0, TRUSTED: 1, OWNER: 2}


# How the speaker was identified.
#
# LOCAL_SESSION is the strongest thing this code can honestly report: the request
# carried no speaker claim at all, so it came from the desktop session the owner
# is sitting in front of. SELF_DECLARED means the request body named someone; that
# is a claim and nothing more.
LOCAL_SESSION = "local_session"
SELF_DECLARED = "self_declared"


# Relationship words this project's users actually say, mapped to one spelling.
# Telugu and Hindi kinship terms are in here because they are what gets spoken --
# "amma" far more often than "mother".
RELATIONSHIP_ALIASES = {
    "amma": "mother",
    "mom": "mother",
    "mummy": "mother",
    "dad": "father",
    "nanna": "father",
    "self": "owner",
    "me": "owner",
    "myself": "owner",
    "teacher": "professor",
    "mentor": "professor",
    "college friend": "friend",
    "classmate": "friend",
}

_OWNER_RELATIONSHIPS = {"owner", "self", "me", "myself"}

_TRUSTED_RELATIONSHIPS = {
    "mother",
    "mom",
    "mummy",
    "amma",
    "father",
    "dad",
    "nanna",
    "sister",
    "brother",
    "wife",
    "husband",
    "friend",
}

# Relationships close enough that the assistant should sound familiar rather than
# formal. Separate from access level on purpose: warmth and authority are not the
# same axis, and conflating them is how a friendly guest ends up with owner rights.
_CLOSE_RELATIONSHIPS = {"owner", "mother", "father"}

# Fields on the owner's profile that are personal to the owner. None of these may
# appear on a resolved profile for anybody else.
OWNER_PRIVATE_FIELDS = (
    "notes",
    "context_profile",
    "conversation_summary",
    "display_name",
)


def normalize_relationship(relationship: str | None) -> str:
    """Canonical spelling of a claimed relationship, or "" when none was claimed."""
    normalized = (relationship or "").strip().lower()
    return RELATIONSHIP_ALIASES.get(normalized, normalized)


def access_level_for_relationship(relationship: str | None) -> str:
    """The authority a relationship earns, before any verification is considered.

    Unknown and unstated relationships both land on `GUEST`. That is the whole
    point: the default has to be the least authority, not the most.
    """
    normalized = normalize_relationship(relationship)
    if normalized in _OWNER_RELATIONSHIPS:
        return OWNER
    if normalized in _TRUSTED_RELATIONSHIPS:
        return TRUSTED
    return GUEST


def at_least(access_level: str | None, required: str) -> bool:
    """Whether `access_level` meets or exceeds `required`."""
    return _LEVEL_RANK.get((access_level or "").strip().lower(), 0) >= _LEVEL_RANK[required]


def closeness_for(relationship: str | None, claimed: str | None = None) -> str:
    """Familiarity level. A claim is honoured only where it cannot grant authority."""
    normalized = normalize_relationship(relationship)
    if normalized in _CLOSE_RELATIONSHIPS:
        return "close"
    stated = (claimed or "").strip().lower()
    if stated in {"close", "normal", "distant", "new"}:
        return stated
    return "normal" if normalized in _TRUSTED_RELATIONSHIPS else "new"


def resolve_speaker_identity(
    claim: dict[str, Any] | None,
    owner_defaults: dict[str, Any],
) -> dict[str, Any]:
    """Resolve who is speaking into a profile with derived, not inherited, authority.

    `claim` is the untrusted `speaker_profile` from the request body. `owner_defaults`
    is the local owner's profile, built from the database.

    No claim means the request came from the local desktop session with nobody
    else named, which is the ordinary case -- the shipped frontend sends no
    `speaker_profile` on any request -- and resolves to the owner unchanged. That
    is deliberate: this is a single-user desktop assistant and locking out the
    person sitting at the machine would be a regression, not a hardening.

    A claim is treated as a claim. Authority comes from
    `access_level_for_relationship`, so a request naming a friend gets `trusted`
    and a request naming nobody in particular gets `guest`, regardless of what
    `access_level` the body asked for.
    """
    if not claim:
        resolved = dict(owner_defaults)
        resolved["access_level"] = OWNER
        resolved["relationship_to_owner"] = OWNER
        resolved["identification_method"] = LOCAL_SESSION
        resolved["identity_verified"] = False
        return resolved

    relationship = normalize_relationship(claim.get("relationship_to_owner"))
    access_level = access_level_for_relationship(relationship)

    if access_level == OWNER:
        # A claim of ownership from the local session. Same authority as no claim
        # at all -- there is nothing here that could verify it either way, so
        # pretending the claim added confidence would be dishonest.
        resolved = {**owner_defaults, **claim}
        resolved["relationship_to_owner"] = OWNER
        resolved["access_level"] = OWNER
        resolved["closeness_level"] = "close"
    else:
        # Built from the claim, not layered over the owner. Anything the claim
        # does not provide is absent rather than inherited, which is what keeps
        # the owner's bio, project notes and running summary out of a visitor's
        # profile.
        resolved = {key: value for key, value in claim.items() if key not in {"access_level"}}
        resolved["relationship_to_owner"] = relationship or None
        resolved["access_level"] = access_level
        resolved["closeness_level"] = closeness_for(relationship, claim.get("closeness_level"))
        resolved.setdefault("display_name", "")
        # Language is a device setting rather than something personal, so falling
        # back to the owner's configured voice language is safe and avoids
        # answering a Telugu speaker in English by default.
        resolved.setdefault("language_preference", owner_defaults.get("language_preference") or "english")

    resolved["identification_method"] = SELF_DECLARED
    resolved["identity_verified"] = False
    return resolved


def owner_context_leaked_into(profile: dict[str, Any], owner_defaults: dict[str, Any]) -> list[str]:
    """Which of the owner's private fields a non-owner profile is carrying.

    Used by the regression tests. Returned as a list of field names rather than a
    bool so a failure says which field leaked instead of only that one did.
    """
    if at_least(profile.get("access_level"), OWNER):
        return []

    leaked = []
    for field in OWNER_PRIVATE_FIELDS:
        owner_value = owner_defaults.get(field)
        if owner_value and profile.get(field) == owner_value:
            leaked.append(field)
    return leaked
