'use client';

import React from 'react';
import AppLayout from '@/components/AppLayout';
import ChatWorkspace from './ChatWorkspace';

export default function ChatInterfaceClient() {
  return (
    <AppLayout activePath="/chat-interface">
      <ChatWorkspace />
    </AppLayout>
  );
}
