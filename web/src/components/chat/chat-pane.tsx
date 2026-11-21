import { ChatComposer } from "./chat-composer.tsx";
import { ChatHeader } from "./chat-header.tsx";
import { MessageList } from "./message-list.tsx";
import { OfflineBanner } from "./offline-banner.tsx";

export function ChatPane() {
  return (
    <div className="flex h-full min-h-0 min-w-0 flex-1 flex-col">
      <ChatHeader />
      <OfflineBanner />
      <MessageList />
      <ChatComposer />
    </div>
  );
}
