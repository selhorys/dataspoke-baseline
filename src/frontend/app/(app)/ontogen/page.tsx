"use client";

import { useState } from "react";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Button } from "@/components/ui/button";
import { NodesPanel } from "@/components/ontogen/nodes-panel";
import { EdgesPanel } from "@/components/ontogen/edges-panel";
import { TriplesPanel } from "@/components/ontogen/triples-panel";
import { RunDialog } from "@/components/ontogen/run-dialog";
import { OntologyNavigator } from "@/components/ontology-navigator";
import { useRunOntogen } from "@/lib/api/ontogen";
import { useMe } from "@/lib/auth/use-me";
import { useToast } from "@/components/ui/use-toast";

export default function OntogenPage() {
  const { canWrite } = useMe();
  const [runDialogOpen, setRunDialogOpen] = useState(false);
  const runMutation = useRunOntogen();
  const { toast } = useToast();

  function handleRun(params: { promptMd?: string; dry_run: boolean }) {
    runMutation.mutate(params, {
      onSuccess: (data) => {
        setRunDialogOpen(false);
        const label = data.dry_run ? "Dry run complete" : "Run complete";
        const detail = Object.entries(data.counts)
          .map(([k, v]) => `${k}: ${v}`)
          .join(", ");
        toast({
          title: label,
          description: detail || data.status,
        });
      },
      onError: (err) => {
        toast({ title: "Run failed", description: err.message, variant: "destructive" });
      },
    });
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-semibold tracking-tight">Ontology Generation</h1>
        {canWrite && (
          <Button onClick={() => setRunDialogOpen(true)} disabled={runMutation.isPending}>
            {runMutation.isPending ? "Running…" : "Run"}
          </Button>
        )}
      </div>

      <Tabs defaultValue="nodes">
        <TabsList>
          <TabsTrigger value="nodes">Nodes</TabsTrigger>
          <TabsTrigger value="edges">Edges</TabsTrigger>
          <TabsTrigger value="triples">Triples</TabsTrigger>
          <TabsTrigger value="navigator">Navigator</TabsTrigger>
        </TabsList>

        <TabsContent value="nodes" className="mt-4">
          <NodesPanel canWrite={canWrite} />
        </TabsContent>

        <TabsContent value="edges" className="mt-4">
          <EdgesPanel canWrite={canWrite} />
        </TabsContent>

        <TabsContent value="triples" className="mt-4">
          <TriplesPanel canWrite={canWrite} />
        </TabsContent>

        <TabsContent value="navigator" className="mt-4">
          <OntologyNavigator canWrite={canWrite} />
        </TabsContent>
      </Tabs>

      <RunDialog
        open={runDialogOpen}
        onOpenChange={setRunDialogOpen}
        onRun={handleRun}
        isRunning={runMutation.isPending}
      />
    </div>
  );
}
