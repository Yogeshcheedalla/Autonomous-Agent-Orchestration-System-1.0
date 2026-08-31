import React from 'react';
import AppLayout from '@/components/AppLayout';
import ConversationHistoryScreen from './components/ConversationHistoryScreen';

export default function ConversationHistoryPage() {
  return (
    <AppLayout activePath="/conversation-history">
      <ConversationHistoryScreen />
    </AppLayout>
  );
}
