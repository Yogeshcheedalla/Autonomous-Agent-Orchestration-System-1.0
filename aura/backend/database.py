import os
from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime, Float, Boolean, text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from datetime import datetime

DATABASE_URL = "sqlite:///./akansha.db"

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

class ChatMessage(Base):
    __tablename__ = "chat_messages"
    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(String, index=True, default="default")
    role = Column(String, index=True) # 'user' or 'assistant'
    content = Column(Text)
    timestamp = Column(DateTime, default=datetime.utcnow)
    pinned = Column(Boolean, default=False, index=True)
    display_order = Column(Float, nullable=True, index=True)
    branch_from_id = Column(Integer, nullable=True, index=True)

class Memory(Base):
    __tablename__ = "memories"
    id = Column(Integer, primary_key=True, index=True)
    topic = Column(String, index=True)
    insight = Column(Text)
    importance = Column(Integer, default=1)
    timestamp = Column(DateTime, default=datetime.utcnow)

class Task(Base):
    __tablename__ = "tasks"
    id = Column(Integer, primary_key=True, index=True)
    title = Column(String)
    description = Column(Text, nullable=True)
    is_completed = Column(Boolean, default=False)
    due_date = Column(DateTime, nullable=True)
    timestamp = Column(DateTime, default=datetime.utcnow)

class InboxMessage(Base):
    __tablename__ = "inbox_messages"
    id = Column(Integer, primary_key=True, index=True)
    platform = Column(String) # 'telegram', 'twitter', 'email'
    sender = Column(String)
    content = Column(Text)
    intent = Column(String, nullable=True)
    sentiment = Column(String, nullable=True)
    is_read = Column(Boolean, default=False)
    timestamp = Column(DateTime, default=datetime.utcnow)

class UserAccount(Base):
    __tablename__ = "user_accounts"
    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, index=True)
    hashed_password = Column(String, nullable=True)
    full_name = Column(String, default="User")
    email_verified = Column(Boolean, default=False)
    verification_token = Column(String, nullable=True, index=True)
    reset_token = Column(String, nullable=True, index=True)
    reset_token_expires_at = Column(DateTime, nullable=True)
    failed_login_attempts = Column(Integer, default=0)
    locked_until = Column(DateTime, nullable=True)
    auth_provider = Column(String, default="email")  # 'email', 'google', 'github', 'passkey'
    passkey_credential_id = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class RefreshTokenRecord(Base):
    __tablename__ = "refresh_token_records"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, index=True)
    token_hash = Column(String, unique=True, index=True)
    device_info = Column(String, default="Unknown Device")
    ip_address = Column(String, nullable=True)
    expires_at = Column(DateTime, index=True)
    is_revoked = Column(Boolean, default=False, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)

class UserProfile(Base):
    __tablename__ = "user_profiles"
    id = Column(Integer, primary_key=True, index=True)
    full_name = Column(String, default="Arjun Mehta")
    email = Column(String, default="arjun.mehta@devcraft.io")
    bio = Column(Text, default="B.Tech student and AI enthusiast. Building the future with Akansha.")
    preferred_mode = Column(String, default="hybrid")
    voice_gender = Column(String, default="female")
    voice_tone = Column(String, default="friendly")
    voice_language = Column(String, default="telugu_english")
    avatar_style = Column(String, default="companion")
    background_listening = Column(Boolean, default=False)
    interrupt_enabled = Column(Boolean, default=True)
    google_connected = Column(Boolean, default=False)
    google_email = Column(String, nullable=True)
    username = Column(String, nullable=True)
    password = Column(String, nullable=True)
    timestamp = Column(DateTime, default=datetime.utcnow)

class IntegrationConnection(Base):
    __tablename__ = "integration_connections"
    id = Column(Integer, primary_key=True, index=True)
    provider = Column(String, index=True, unique=True)
    access_token = Column(Text, nullable=True)
    refresh_token = Column(Text, nullable=True)
    scope = Column(Text, nullable=True)
    account_email = Column(String, nullable=True)
    token_expiry = Column(DateTime, nullable=True)
    metadata_json = Column(Text, nullable=True)
    is_connected = Column(Boolean, default=False)
    timestamp = Column(DateTime, default=datetime.utcnow)

class OwnerSecret(Base):
    """What proves the boss is the boss, kept out of every serialiser.

    Deliberately its own table rather than more columns on `speaker_profiles`:
    that row is returned wholesale by `serialize_speaker_profile`, and a
    passphrase hash or a voiceprint vector that rides out to a browser is a
    forgeable factor. Nothing here is ever included in an API response -- the
    owner endpoints report *whether* a factor is enrolled, never its value.
    """

    __tablename__ = "owner_secrets"
    id = Column(Integer, primary_key=True, index=True)
    key = Column(String, index=True, unique=True)  # passphrase | voiceprint | pending_challenge
    value_json = Column(Text, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class SpeakerProfile(Base):
    __tablename__ = "speaker_profiles"
    id = Column(Integer, primary_key=True, index=True)
    display_name = Column(String, index=True)
    relationship_to_owner = Column(String, nullable=True)
    access_level = Column(String, default="guest")  # owner | trusted | guest
    closeness_level = Column(String, default="normal")  # close | normal | distant | new
    communication_style = Column(String, nullable=True)
    language_preference = Column(String, nullable=True)
    notes = Column(Text, nullable=True)
    context_profile_json = Column(Text, nullable=True)
    conversation_summary = Column(Text, nullable=True)
    mood_state = Column(String, nullable=True)
    interaction_count = Column(Integer, default=0)
    last_intro_text = Column(Text, nullable=True)
    last_heard_text = Column(Text, nullable=True)
    voice_signature_json = Column(Text, nullable=True)
    timestamp = Column(DateTime, default=datetime.utcnow)


class SpeakerInteraction(Base):
    __tablename__ = "speaker_interactions"
    id = Column(Integer, primary_key=True, index=True)
    speaker_id = Column(Integer, nullable=True, index=True)
    speaker_name = Column(String, index=True)
    session_id = Column(String, index=True, default="default")
    role = Column(String, index=True)  # user | assistant
    content = Column(Text)
    mood_state = Column(String, nullable=True)
    timestamp = Column(DateTime, default=datetime.utcnow)

Base.metadata.create_all(bind=engine)


def ensure_profile_columns():
    with engine.begin() as connection:
        try:
            column_rows = connection.execute(text("PRAGMA table_info(user_profiles)")).fetchall()
        except Exception:
            return

        existing_columns = {row[1] for row in column_rows}
        if "voice_language" not in existing_columns:
            connection.execute(text("ALTER TABLE user_profiles ADD COLUMN voice_language VARCHAR DEFAULT 'telugu_english'"))
        if "username" not in existing_columns:
            connection.execute(text("ALTER TABLE user_profiles ADD COLUMN username VARCHAR"))
        if "password" not in existing_columns:
            connection.execute(text("ALTER TABLE user_profiles ADD COLUMN password VARCHAR"))
        if "bio" not in existing_columns:
            connection.execute(
                text(
                    "ALTER TABLE user_profiles ADD COLUMN bio TEXT DEFAULT 'B.Tech student and AI enthusiast. Building the future with Akansha.'"
                )
            )


ensure_profile_columns()


def ensure_speaker_columns():
    with engine.begin() as connection:
        try:
            column_rows = connection.execute(text("PRAGMA table_info(speaker_profiles)")).fetchall()
        except Exception:
            return

        existing_columns = {row[1] for row in column_rows}
        if "last_heard_text" not in existing_columns:
            connection.execute(text("ALTER TABLE speaker_profiles ADD COLUMN last_heard_text TEXT"))
        if "voice_signature_json" not in existing_columns:
            connection.execute(text("ALTER TABLE speaker_profiles ADD COLUMN voice_signature_json TEXT"))
        if "closeness_level" not in existing_columns:
            connection.execute(text("ALTER TABLE speaker_profiles ADD COLUMN closeness_level VARCHAR DEFAULT 'normal'"))
        if "communication_style" not in existing_columns:
            connection.execute(text("ALTER TABLE speaker_profiles ADD COLUMN communication_style VARCHAR"))
        if "language_preference" not in existing_columns:
            connection.execute(text("ALTER TABLE speaker_profiles ADD COLUMN language_preference VARCHAR"))
        if "context_profile_json" not in existing_columns:
            connection.execute(text("ALTER TABLE speaker_profiles ADD COLUMN context_profile_json TEXT"))
        if "conversation_summary" not in existing_columns:
            connection.execute(text("ALTER TABLE speaker_profiles ADD COLUMN conversation_summary TEXT"))
        if "mood_state" not in existing_columns:
            connection.execute(text("ALTER TABLE speaker_profiles ADD COLUMN mood_state VARCHAR"))
        if "interaction_count" not in existing_columns:
            connection.execute(text("ALTER TABLE speaker_profiles ADD COLUMN interaction_count INTEGER DEFAULT 0"))


ensure_speaker_columns()


def ensure_speaker_interaction_columns():
    with engine.begin() as connection:
        try:
            column_rows = connection.execute(text("PRAGMA table_info(speaker_interactions)")).fetchall()
        except Exception:
            Base.metadata.create_all(bind=engine)
            return

        if not column_rows:
            Base.metadata.create_all(bind=engine)
            return

        existing_columns = {row[1] for row in column_rows}
        if "speaker_id" not in existing_columns:
            connection.execute(text("ALTER TABLE speaker_interactions ADD COLUMN speaker_id INTEGER"))
        if "speaker_name" not in existing_columns:
            connection.execute(text("ALTER TABLE speaker_interactions ADD COLUMN speaker_name VARCHAR"))
        if "session_id" not in existing_columns:
            connection.execute(text("ALTER TABLE speaker_interactions ADD COLUMN session_id VARCHAR DEFAULT 'default'"))
        if "role" not in existing_columns:
            connection.execute(text("ALTER TABLE speaker_interactions ADD COLUMN role VARCHAR"))
        if "content" not in existing_columns:
            connection.execute(text("ALTER TABLE speaker_interactions ADD COLUMN content TEXT"))
        if "mood_state" not in existing_columns:
            connection.execute(text("ALTER TABLE speaker_interactions ADD COLUMN mood_state VARCHAR"))


ensure_speaker_interaction_columns()


def ensure_chat_message_columns():
    with engine.begin() as connection:
        try:
            column_rows = connection.execute(text("PRAGMA table_info(chat_messages)")).fetchall()
        except Exception:
            return

        existing_columns = {row[1] for row in column_rows}
        if "pinned" not in existing_columns:
            connection.execute(text("ALTER TABLE chat_messages ADD COLUMN pinned BOOLEAN DEFAULT 0"))
        if "display_order" not in existing_columns:
            connection.execute(text("ALTER TABLE chat_messages ADD COLUMN display_order FLOAT"))
            connection.execute(text("UPDATE chat_messages SET display_order = id WHERE display_order IS NULL"))
        if "branch_from_id" not in existing_columns:
            connection.execute(text("ALTER TABLE chat_messages ADD COLUMN branch_from_id INTEGER"))


ensure_chat_message_columns()


class TaskAutomation(Base):
    __tablename__ = "task_automations"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, index=True)
    description = Column(Text, nullable=True)
    trigger_type = Column(String, index=True, default="schedule")  # 'schedule' | 'webhook' | 'event' | 'manual'
    trigger_config = Column(Text, nullable=True)  # JSON: { interval_minutes: 60, cron: "0 9 * * *", webhook_key: "wh_xxx" }
    action_type = Column(String, default="ai_workflow")  # 'github_decompose' | 'daily_report' | 'desktop_action' | 'ai_workflow' | 'custom_agent'
    action_payload = Column(Text, nullable=True)  # JSON: { prompt: "...", repo: "...", channel: "..." }
    status = Column(String, index=True, default="active")  # 'active' | 'paused' | 'running' | 'error'
    last_run_at = Column(DateTime, nullable=True)
    next_run_at = Column(DateTime, nullable=True)
    run_count = Column(Integer, default=0)
    timestamp = Column(DateTime, default=datetime.utcnow)


class AutomationExecutionLog(Base):
    __tablename__ = "automation_execution_logs"
    id = Column(Integer, primary_key=True, index=True)
    automation_id = Column(Integer, index=True)
    automation_name = Column(String)
    trigger_source = Column(String)  # 'schedule' | 'webhook:github' | 'manual'
    status = Column(String, index=True)  # 'success' | 'failed' | 'running'
    output_summary = Column(Text, nullable=True)
    details_json = Column(Text, nullable=True)
    started_at = Column(DateTime, default=datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)


def ensure_task_automation_tables():
    with engine.begin() as connection:
        try:
            Base.metadata.create_all(bind=connection)
        except Exception:
            pass


ensure_task_automation_tables()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

