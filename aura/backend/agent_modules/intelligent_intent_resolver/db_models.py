"""Database models for the Intelligent Intent Resolver."""
from __future__ import annotations
import time
from sqlalchemy import Column, Integer, String, Float, Boolean, Index
from sqlalchemy.ext.declarative import declarative_base

Base = declarative_base()


class UserIntentHistory(Base):
    __tablename__ = "user_intent_history"
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String, nullable=False)
    raw_input = Column(String, nullable=False)
    resolved_entity = Column(String, nullable=False)
    entity_type = Column(String, nullable=False)
    canonical_url = Column(String, nullable=True)
    confidence = Column(Float, nullable=False)
    user_confirmed = Column(Boolean, nullable=True)
    timestamp = Column(Float, nullable=False, default=time.time)
    language = Column(String, nullable=True)
    __table_args__ = (Index("idx_user_timestamp", "user_id", "timestamp"),)


class UserCorrections(Base):
    __tablename__ = "user_corrections"
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String, nullable=False)
    misheard_input = Column(String, nullable=False)
    correct_entity = Column(String, nullable=False)
    correction_count = Column(Integer, default=1)
    last_corrected = Column(Float, nullable=False, default=time.time)
    __table_args__ = (Index("idx_user_misheard", "user_id", "misheard_input"),)
