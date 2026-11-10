"""Начальная схема (то, что раньше создавал create_all)

Revision ID: 0001
Revises: 
Create Date: 2026-11-05 18:56:07.007177
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0001'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('incidents',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('service_id', sa.String(length=64), nullable=False),
    sa.Column('dedup_key', sa.String(length=255), nullable=False),
    sa.Column('title', sa.String(length=500), nullable=False),
    sa.Column('details', sa.Text(), nullable=False),
    sa.Column('severity', sa.String(length=32), nullable=False),
    sa.Column('source', sa.String(length=32), nullable=False),
    sa.Column('labels', sa.JSON(), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('last_alert_at', sa.DateTime(), nullable=False),
    sa.Column('alert_count', sa.Integer(), nullable=False),
    sa.Column('acknowledged_at', sa.DateTime(), nullable=True),
    sa.Column('acknowledged_by', sa.String(length=128), nullable=True),
    sa.Column('resolved_at', sa.DateTime(), nullable=True),
    sa.Column('resolved_by', sa.String(length=128), nullable=True),
    sa.Column('escalation_step', sa.Integer(), nullable=False),
    sa.Column('escalation_loop', sa.Integer(), nullable=False),
    sa.Column('next_escalation_at', sa.DateTime(), nullable=True),
    sa.Column('paged_users', sa.JSON(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('incidents', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_incidents_dedup_key'), ['dedup_key'], unique=False)
        batch_op.create_index(batch_op.f('ix_incidents_next_escalation_at'), ['next_escalation_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_incidents_service_id'), ['service_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_incidents_status'), ['status'], unique=False)

    op.create_table('kv',
    sa.Column('key', sa.String(length=64), nullable=False),
    sa.Column('value', sa.Text(), nullable=False),
    sa.PrimaryKeyConstraint('key'),
    sa.UniqueConstraint('key')
    )
    op.create_table('schedule_overrides',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('schedule_id', sa.String(length=64), nullable=False),
    sa.Column('user_id', sa.String(length=64), nullable=False),
    sa.Column('start', sa.DateTime(), nullable=False),
    sa.Column('end', sa.DateTime(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('schedule_overrides', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_schedule_overrides_schedule_id'), ['schedule_id'], unique=False)

    op.create_table('telegram_links',
    sa.Column('user_id', sa.String(length=64), nullable=False),
    sa.Column('chat_id', sa.BigInteger(), nullable=False),
    sa.Column('tg_user_id', sa.BigInteger(), nullable=False),
    sa.Column('tg_username', sa.String(length=64), nullable=True),
    sa.Column('linked_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('user_id'),
    sa.UniqueConstraint('chat_id'),
    sa.UniqueConstraint('tg_user_id')
    )
    op.create_table('incident_events',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('incident_id', sa.Integer(), nullable=False),
    sa.Column('at', sa.DateTime(), nullable=False),
    sa.Column('kind', sa.String(length=32), nullable=False),
    sa.Column('actor', sa.String(length=128), nullable=True),
    sa.Column('message', sa.Text(), nullable=False),
    sa.ForeignKeyConstraint(['incident_id'], ['incidents.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('incident_events', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_incident_events_incident_id'), ['incident_id'], unique=False)

    op.create_table('sent_messages',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('incident_id', sa.Integer(), nullable=False),
    sa.Column('chat_id', sa.BigInteger(), nullable=False),
    sa.Column('message_id', sa.BigInteger(), nullable=False),
    sa.ForeignKeyConstraint(['incident_id'], ['incidents.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('sent_messages', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_sent_messages_incident_id'), ['incident_id'], unique=False)



def downgrade() -> None:
    with op.batch_alter_table('sent_messages', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_sent_messages_incident_id'))

    op.drop_table('sent_messages')
    with op.batch_alter_table('incident_events', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_incident_events_incident_id'))

    op.drop_table('incident_events')
    op.drop_table('telegram_links')
    with op.batch_alter_table('schedule_overrides', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_schedule_overrides_schedule_id'))

    op.drop_table('schedule_overrides')
    op.drop_table('kv')
    with op.batch_alter_table('incidents', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_incidents_status'))
        batch_op.drop_index(batch_op.f('ix_incidents_service_id'))
        batch_op.drop_index(batch_op.f('ix_incidents_next_escalation_at'))
        batch_op.drop_index(batch_op.f('ix_incidents_dedup_key'))

    op.drop_table('incidents')
