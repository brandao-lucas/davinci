'use client';

import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Separator } from '@/components/ui/separator';
import {
  BarChart,
  Bar,
  XAxis,
  YAxis,
  Tooltip,
  ResponsiveContainer,
  Cell,
} from 'recharts';
import { formatNumber } from '@/lib/utils/format';
import type { MagnitudePreview } from '@/lib/types/advanced-search';

interface MagnitudePanelProps {
  preview: MagnitudePreview;
}

const PUB_TYPE_COLORS = [
  '#2563eb', '#16a34a', '#d97706', '#dc2626', '#7c3aed', '#0891b2',
];

function MetricCard({ label, value, sub, highlight }: {
  label: string;
  value: number | string;
  sub?: string;
  highlight?: boolean;
}) {
  return (
    <Card className={highlight ? 'border-primary/60 bg-primary/5' : undefined}>
      <CardContent className="pt-5 pb-4">
        <p className={`text-2xl font-bold tabular-nums ${highlight ? 'text-primary' : ''}`}>
          {typeof value === 'number' ? formatNumber(value) : value}
        </p>
        <p className="text-xs font-medium mt-0.5">{label}</p>
        {sub && <p className="text-xs text-muted-foreground">{sub}</p>}
      </CardContent>
    </Card>
  );
}

/** Diagrama Venn simplificado em texto — proporções das zonas */
function VennDiagram({ preview }: { preview: MagnitudePreview }) {
  const total = Math.max(preview.combined_count, 1);
  const onlyFtPct = Math.round((preview.only_free_text / total) * 100);
  const overlapPct = Math.round((preview.overlap / total) * 100);
  const onlyMeshPct = Math.round((preview.only_mesh / total) * 100);
  const notIndexedPct = Math.round((preview.not_yet_indexed / total) * 100);

  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-sm">Cobertura — distribuição dos artigos</CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="grid grid-cols-2 gap-2 text-xs">
          <div className="rounded-lg border border-blue-200 bg-blue-50 dark:bg-blue-950/40 dark:border-blue-800 p-2">
            <p className="font-semibold text-blue-700 dark:text-blue-300">
              {formatNumber(preview.only_free_text)}
            </p>
            <p className="text-muted-foreground">Só free-text <span className="font-mono">({onlyFtPct}%)</span></p>
            <p className="mt-0.5 text-muted-foreground/70 text-[10px]">fora do vocabulário controlado</p>
          </div>

          <div className="rounded-lg border border-green-200 bg-green-50 dark:bg-green-950/40 dark:border-green-800 p-2">
            <p className="font-semibold text-green-700 dark:text-green-300">
              {formatNumber(preview.overlap)}
            </p>
            <p className="text-muted-foreground">Overlap <span className="font-mono">({overlapPct}%)</span></p>
            <p className="mt-0.5 text-muted-foreground/70 text-[10px]">cobertos por ambos</p>
          </div>

          <div className="rounded-lg border border-violet-200 bg-violet-50 dark:bg-violet-950/40 dark:border-violet-800 p-2">
            <p className="font-semibold text-violet-700 dark:text-violet-300">
              {formatNumber(preview.only_mesh)}
            </p>
            <p className="text-muted-foreground">Só MeSH <span className="font-mono">({onlyMeshPct}%)</span></p>
            <p className="mt-0.5 text-muted-foreground/70 text-[10px]">adicionados pelo vocabulário</p>
          </div>

          <div className="rounded-lg border border-orange-200 bg-orange-50 dark:bg-orange-950/40 dark:border-orange-800 p-2">
            <p className="font-semibold text-orange-700 dark:text-orange-300">
              {formatNumber(preview.not_yet_indexed)}
            </p>
            <p className="text-muted-foreground">Não indexados <span className="font-mono">({notIndexedPct}%)</span></p>
            <p className="mt-0.5 text-muted-foreground/70 text-[10px]">publisher, sem MeSH ainda</p>
          </div>
        </div>

        <Separator />

        <div className="text-xs text-muted-foreground">
          <p className="font-mono break-all leading-relaxed">{preview.query_used}</p>
          <p className="mt-1 text-[10px]">Query exata que será usada na ingestão</p>
        </div>
      </CardContent>
    </Card>
  );
}

/** Gráfico de barras por ano */
function ByYearChart({ byYear }: { byYear: number[][] }) {
  const data = byYear.map(([year, count]) => ({ year: String(year), count }));

  if (data.length === 0) return null;

  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-sm">Publicações por ano</CardTitle>
      </CardHeader>
      <CardContent>
        <ResponsiveContainer width="100%" height={180}>
          <BarChart data={data}>
            <XAxis dataKey="year" tick={{ fontSize: 10 }} />
            <YAxis tick={{ fontSize: 10 }} />
            <Tooltip
              formatter={(v) => [typeof v === 'number' ? formatNumber(v) : v, 'Artigos']}
            />
            <Bar dataKey="count" fill="#0c93e7" radius={[2, 2, 0, 0]} />
          </BarChart>
        </ResponsiveContainer>
      </CardContent>
    </Card>
  );
}

/** Breakdown de tipos de publicação */
function ByPubTypeChart({ byPubType }: { byPubType: unknown[][] }) {
  // byPubType: Array of [string, number]
  const data = byPubType
    .filter((item): item is [string, number] => Array.isArray(item) && item.length === 2)
    .map(([name, count]) => ({ name: String(name), count: Number(count) }));

  if (data.length === 0) return null;

  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-sm">Tipos de publicação</CardTitle>
      </CardHeader>
      <CardContent>
        <ResponsiveContainer width="100%" height={160}>
          <BarChart data={data} layout="vertical">
            <XAxis type="number" tick={{ fontSize: 10 }} />
            <YAxis type="category" dataKey="name" tick={{ fontSize: 10 }} width={110} />
            <Tooltip formatter={(v) => [typeof v === 'number' ? formatNumber(v) : v, 'Artigos']} />
            <Bar dataKey="count" radius={[0, 2, 2, 0]}>
              {data.map((_, i) => (
                <Cell key={i} fill={PUB_TYPE_COLORS[i % PUB_TYPE_COLORS.length]} />
              ))}
            </Bar>
          </BarChart>
        </ResponsiveContainer>
      </CardContent>
    </Card>
  );
}

/** Cards de acesso aberto */
function OpenAccessCard({ openAccess }: { openAccess: number[] }) {
  // openAccess: [free_full_text_count, pmc_count]
  const [fft, pmc] = openAccess;

  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-sm">Acesso aberto</CardTitle>
      </CardHeader>
      <CardContent className="grid grid-cols-2 gap-3">
        <div className="rounded-lg border border-teal-200 bg-teal-50 dark:bg-teal-950/40 dark:border-teal-800 p-3">
          <p className="text-xl font-bold text-teal-700 dark:text-teal-300 tabular-nums">
            {formatNumber(fft)}
          </p>
          <p className="text-xs text-muted-foreground mt-0.5">Free full text</p>
        </div>
        <div className="rounded-lg border border-cyan-200 bg-cyan-50 dark:bg-cyan-950/40 dark:border-cyan-800 p-3">
          <p className="text-xl font-bold text-cyan-700 dark:text-cyan-300 tabular-nums">
            {formatNumber(pmc)}
          </p>
          <p className="text-xs text-muted-foreground mt-0.5">PubMed Central</p>
        </div>
      </CardContent>
    </Card>
  );
}

export function MagnitudePanel({ preview }: MagnitudePanelProps) {
  const coveragePct =
    preview.free_text_count > 0
      ? Math.round((preview.combined_count / preview.free_text_count) * 100)
      : 0;

  return (
    <div className="space-y-4">
      {/* Core counts */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <MetricCard
          label="Free-text"
          value={preview.free_text_count}
          sub="busca por termo livre"
        />
        <MetricCard
          label="MeSH"
          value={preview.mesh_count}
          sub="vocabulário controlado"
        />
        <MetricCard
          label="Combinado"
          value={preview.combined_count}
          sub="query final (será ingerido)"
          highlight
        />
        <MetricCard
          label="Cobertura MeSH"
          value={`${coveragePct}%`}
          sub="combinado / free-text"
        />
      </div>

      {/* Reviews */}
      <div className="grid grid-cols-2 gap-3">
        <MetricCard
          label="Reviews"
          value={preview.reviews}
          sub="Review[pt]"
        />
        <MetricCard
          label="Revisões sistemáticas"
          value={preview.systematic_reviews}
          sub="systematic[sb] ou Meta-Analysis[pt]"
        />
      </div>

      {/* Venn */}
      <VennDiagram preview={preview} />

      {/* Heavy panel — só renderiza quando dados presentes */}
      {preview.by_year && preview.by_year.length > 0 && (
        <ByYearChart byYear={preview.by_year} />
      )}

      {preview.by_pub_type && preview.by_pub_type.length > 0 && (
        <ByPubTypeChart byPubType={preview.by_pub_type} />
      )}

      {preview.open_access && preview.open_access.length >= 2 && (
        <OpenAccessCard openAccess={preview.open_access} />
      )}
    </div>
  );
}
