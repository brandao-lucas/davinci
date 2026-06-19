'use client';

import { use, useState } from 'react';
import Link from 'next/link';
import { PageHeader } from '@/components/layout/page-header';
import { ProjectStatsOverview } from '@/components/projects/project-stats-overview';
import { PapersTable } from '@/components/papers/papers-table';
import { DatasetsTable } from '@/components/datasets/datasets-table';
import { SamplesTable } from '@/components/samples/samples-table';
import { SampleDetailPanel } from '@/components/samples/sample-detail-panel';
import { PaperDetailPanel } from '@/components/papers/paper-detail-panel';
import { DatasetDetailPanel } from '@/components/datasets/dataset-detail-panel';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { useProjectStats } from '@/lib/hooks/use-projects';
import { usePapers, usePaper } from '@/lib/hooks/use-papers';
import { useDatasets } from '@/lib/hooks/use-datasets';
import { useSamplesByProject } from '@/lib/hooks/use-samples';
import type { Paper } from '@/lib/types/paper';
import type { OmicDataset } from '@/lib/types/dataset';
import type { ProjectSample } from '@/lib/types/sample';

const INCLUDED_PAPERS_FILTER = { curation_status: 'included' };
const INCLUDED_DATASETS_FILTER = { curation_status: 'included' };
const INCLUDED_SAMPLES_FILTER = { curation_status: 'included' };

export default function AnalysisPage({ params }: { params: Promise<{ projectId: string }> }) {
  const { projectId } = use(params);

  // Stats
  const { data: stats, isLoading: statsLoading } = useProjectStats(projectId);

  // Included papers
  const { data: papersData, isLoading: papersLoading } = usePapers(projectId, INCLUDED_PAPERS_FILTER);
  const includedPapers = papersData?.results ?? [];

  // Included datasets
  const { data: datasetsData, isLoading: datasetsLoading } = useDatasets(projectId, INCLUDED_DATASETS_FILTER);
  const includedDatasets = datasetsData?.results ?? [];

  // Included samples
  const { data: samplesData, isLoading: samplesLoading } = useSamplesByProject(projectId, INCLUDED_SAMPLES_FILTER);
  const includedSamples = samplesData?.results ?? [];

  // Detail panels
  const [selectedPaperId, setSelectedPaperId] = useState<number | null>(null);
  const [selectedDataset, setSelectedDataset] = useState<OmicDataset | null>(null);
  const [selectedSample, setSelectedSample] = useState<ProjectSample | null>(null);

  const { data: paperDetail, isLoading: detailLoading } = usePaper(
    projectId,
    selectedPaperId ?? 0,
  );

  const handleSelectPaper = (paper: Paper) => setSelectedPaperId(paper.id);
  const handleCloseDetail = () => setSelectedPaperId(null);

  return (
    <div className="space-y-6">
      <PageHeader title="Analysis" description="Itens curados e estatísticas do projeto" />

      <Tabs defaultValue="overview">
        <TabsList>
          <TabsTrigger value="overview">Overview</TabsTrigger>
          <TabsTrigger value="papers">
            Included Papers
            {papersData?.count != null && (
              <Badge variant="secondary" className="ml-2">{papersData.count}</Badge>
            )}
          </TabsTrigger>
          <TabsTrigger value="datasets">
            Included Datasets
            {datasetsData?.count != null && (
              <Badge variant="secondary" className="ml-2">{datasetsData.count}</Badge>
            )}
          </TabsTrigger>
          <TabsTrigger value="samples">
            Included Samples
            {samplesData?.count != null && (
              <Badge variant="secondary" className="ml-2">{samplesData.count}</Badge>
            )}
          </TabsTrigger>
        </TabsList>

        {/* ── Overview ── */}
        <TabsContent value="overview" className="mt-6 space-y-6">
          {statsLoading ? (
            <div className="h-64 bg-muted rounded-lg animate-pulse" />
          ) : !stats ? (
            <p className="text-muted-foreground">No stats available yet.</p>
          ) : (
            <>
              <ProjectStatsOverview stats={stats} projectId={projectId} />

              <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
                <Card>
                  <CardHeader>
                    <CardTitle className="text-sm">
                      <Link
                        href={`/projects/${projectId}/genes`}
                        className="hover:underline text-foreground"
                      >
                        Top Genes
                      </Link>
                    </CardTitle>
                  </CardHeader>
                  <CardContent>
                    <div className="flex flex-wrap gap-2">
                      {stats.top_genes.map((g, i) => (
                        <Badge key={i} variant="outline" className="font-mono">
                          {g.gene} <span className="ml-1 text-muted-foreground">×{g.count}</span>
                        </Badge>
                      ))}
                    </div>
                  </CardContent>
                </Card>

                <Card>
                  <CardHeader>
                    <CardTitle className="text-sm">
                      <Link
                        href={`/projects/${projectId}/mesh`}
                        className="hover:underline text-foreground"
                      >
                        Top MeSH Terms
                      </Link>
                    </CardTitle>
                  </CardHeader>
                  <CardContent>
                    <div className="flex flex-wrap gap-2">
                      {stats.top_mesh_terms.map((m, i) => (
                        <Badge key={i} variant="secondary">
                          {m.term} <span className="ml-1">×{m.count}</span>
                        </Badge>
                      ))}
                    </div>
                  </CardContent>
                </Card>

                <Card>
                  <CardHeader>
                    <CardTitle className="text-sm">
                      <Link
                        href={`/projects/${projectId}/drugs`}
                        className="hover:underline text-foreground"
                      >
                        Top Drugs
                      </Link>
                    </CardTitle>
                  </CardHeader>
                  <CardContent>
                    <div className="flex flex-wrap gap-2">
                      {stats.top_drugs.map((d, i) => (
                        <Badge key={i} variant="outline">
                          {d.drug} <span className="ml-1 text-muted-foreground">×{d.count}</span>
                        </Badge>
                      ))}
                    </div>
                  </CardContent>
                </Card>
              </div>
            </>
          )}
        </TabsContent>

        {/* ── Included Papers ── */}
        <TabsContent value="papers" className="mt-6">
          {papersLoading ? (
            <div className="h-64 bg-muted rounded-lg animate-pulse" />
          ) : includedPapers.length === 0 ? (
            <p className="text-muted-foreground text-sm">
              Nenhum paper marcado como &quot;included&quot; ainda.
            </p>
          ) : (
            <PapersTable
              papers={includedPapers}
              onSelect={handleSelectPaper}
            />
          )}
        </TabsContent>

        {/* ── Included Datasets ── */}
        <TabsContent value="datasets" className="mt-6">
          {datasetsLoading ? (
            <div className="h-64 bg-muted rounded-lg animate-pulse" />
          ) : includedDatasets.length === 0 ? (
            <p className="text-muted-foreground text-sm">
              Nenhum dataset marcado como &quot;included&quot; ainda.
            </p>
          ) : (
            <DatasetsTable
              datasets={includedDatasets}
              onSelect={setSelectedDataset}
            />
          )}
        </TabsContent>

        {/* ── Included Samples ── */}
        <TabsContent value="samples" className="mt-6">
          {samplesLoading ? (
            <div className="h-64 bg-muted rounded-lg animate-pulse" />
          ) : includedSamples.length === 0 ? (
            <p className="text-muted-foreground text-sm">
              Nenhuma amostra marcada como &quot;included&quot; ainda. Samples são
              ingeridos sob demanda após um dataset ser curado como &quot;included&quot;.
            </p>
          ) : (
            <SamplesTable
              samples={includedSamples}
              onSelect={setSelectedSample}
            />
          )}
        </TabsContent>
      </Tabs>

      {/* Detail panels */}
      <PaperDetailPanel
        paperId={selectedPaperId}
        detail={paperDetail}
        isLoading={detailLoading}
        onClose={handleCloseDetail}
      />
      <DatasetDetailPanel
        dataset={selectedDataset}
        projectId={projectId}
        onClose={() => setSelectedDataset(null)}
      />
      <SampleDetailPanel
        sample={selectedSample}
        projectId={projectId}
        onClose={() => setSelectedSample(null)}
      />
    </div>
  );
}
