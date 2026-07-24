"""users, workspace membership, sessions and invitations

The first real identity in MarkOS. Until now `company_id` arrived as a query
parameter, which meant anyone could read any workspace by typing its name —
honest enough while there was no login, indefensible once there is one. From
here the workspace is read from the SESSION and never from the request.

Four tables:
  users        — a person. Password is bcrypt-hashed; the plaintext never
                 leaves the request handler and is never logged.
  memberships  — which workspaces a person belongs to, and as what. A person
                 can be in several: an agency runs one workspace per client,
                 and a manager may sit across teams.
  sessions     — server-side, so logging out REVOKES rather than politely
                 asking the browser to forget. Holds the active workspace, so
                 switching workspace is a server fact, not a client claim.
  invitations  — how somebody joins a workspace they did not create. Single-use
                 and expiring, the same discipline action_tokens already uses.

Revision ID: 0016_auth
Revises: 0015_workflow_trigger_fires
Create Date: 2026-07-22

"""
from alembic import op

revision = "0016_auth"
down_revision = "0015_workflow_trigger_fires"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id BIGSERIAL PRIMARY KEY,
            email TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            last_login_at TIMESTAMPTZ
        )
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS memberships (
            user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            company_id TEXT NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
            role TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (user_id, company_id)
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_memberships_company ON memberships (company_id)")

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS auth_sessions (
            id BIGSERIAL PRIMARY KEY,
            -- only the HASH is stored: a stolen database cannot be replayed as
            -- a login, exactly as with a password
            token_hash TEXT NOT NULL UNIQUE,
            user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            company_id TEXT NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            expires_at TIMESTAMPTZ NOT NULL,
            revoked_at TIMESTAMPTZ
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_auth_sessions_user ON auth_sessions (user_id)")

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS invitations (
            id BIGSERIAL PRIMARY KEY,
            company_id TEXT NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
            email TEXT NOT NULL,
            role TEXT NOT NULL,
            token_hash TEXT NOT NULL UNIQUE,
            invited_by BIGINT REFERENCES users(id) ON DELETE SET NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            expires_at TIMESTAMPTZ NOT NULL,
            accepted_at TIMESTAMPTZ
        )
        """
    )
    # one LIVE invitation per email per workspace; accepted ones stay as history
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_invitation_open
        ON invitations (company_id, email) WHERE accepted_at IS NULL
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS invitations")
    op.execute("DROP TABLE IF EXISTS auth_sessions")
    op.execute("DROP TABLE IF EXISTS memberships")
    op.execute("DROP TABLE IF EXISTS users")
