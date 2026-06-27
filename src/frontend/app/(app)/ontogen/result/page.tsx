"use client";

import dynamic from "next/dynamic";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Skeleton } from "@/components/ui/skeleton";
import { NodesPanel } from "@/components/ontogen/nodes-panel";
import { EdgesPanel } from "@/components/ontogen/edges-panel";
import { TriplesPanel } from "@/components/ontogen/triples-panel";
import { PageHeader } from "@/components/page-header";
import { useMe } from "@/lib/auth/use-me";

// The graph renders to a canvas and must run only in the browser.
const OntologyGraph = dynamic(
  () => import("@/components/ontogen/ontology-graph").then((m) => m.OntologyGraph),
  {
    ssr: false,
    loading: () => <Skeleton className="h-[560px] w-full rounded-md" />,
  },
);

export default function OntogenResultPage() {
  const { canWrite } = useMe();

  return (
    <div className="space-y-4">
      <PageHeader title="Ontology Generation" />

      <Tabs defaultValue="nodes">
        <TabsList>
          <TabsTrigger value="nodes">Nodes</TabsTrigger>
          <TabsTrigger value="edges">Edges</TabsTrigger>
          <TabsTrigger value="triples">Triples</TabsTrigger>
          <TabsTrigger value="graph">Graph</TabsTrigger>
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

        <TabsContent value="graph" className="mt-4">
          <OntologyGraph />
        </TabsContent>
      </Tabs>
    </div>
  );
}
