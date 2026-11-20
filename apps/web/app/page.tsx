import { ChatApp } from '@/components/ChatApp';

// The whole surface is interactive and session-scoped, so there is nothing to
// prerender. Rendering the client component from a server page keeps the
// initial HTML small.
export const dynamic = 'force-dynamic';

export default function Page() {
  return <ChatApp />;
}
