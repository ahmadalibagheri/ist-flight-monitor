import Dashboard from "@/components/dashboard";

// Rendered on the client so every panel reads live API data rather than a
// build-time snapshot.
export const dynamic = "force-dynamic";

export default function Page() {
  return <Dashboard />;
}
