import Link from 'next/link';
import { Button } from '@/components/ui/button';

export default function LandingPage() {
  return (
    <main className="flex min-h-full flex-col items-center justify-center gap-8 p-8">
      <div className="text-center space-y-3">
        <h1 className="text-5xl font-bold">DaVinci</h1>
        <p className="text-xl text-muted-foreground max-w-lg">
          Biomedical literature & omics curation platform for systematic reviews
        </p>
      </div>
      <div className="flex gap-4">
        <Button asChild size="lg">
          <Link href="/login">Get Started</Link>
        </Button>
        <Button asChild variant="outline" size="lg">
          <Link href="/projects">My Projects</Link>
        </Button>
      </div>
    </main>
  );
}
