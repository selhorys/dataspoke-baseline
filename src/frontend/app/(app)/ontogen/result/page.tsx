"use client";

import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { NodesPanel } from "@/components/ontogen/nodes-panel";
import { EdgesPanel } from "@/components/ontogen/edges-panel";
import { TriplesPanel } from "@/components/ontogen/triples-panel";
import { OntologyNavigator } from "@/components/ontology-navigator";
import { useMe } from "@/lib/auth/use-me";

export default function OntogenResultPage() {
  const { canWrite } = useMe();

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-semibold tracking-tight">Ontology Generation</h1>
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
    </div>
  );
}
