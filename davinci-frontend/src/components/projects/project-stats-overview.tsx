'use client';

import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { BarChart, Bar, XAxis, YAxis, Tooltip, PieChart, Pie, Cell, ResponsiveContainer } from 'recharts';
import { formatNumber } from '@/lib/utils/format';
import type { ProjectStats } from '@/lib/types/project';

const OMIC_COLORS = [
  '#2563eb', '#16a34a', '#d97706', '#dc2626', '#7c3aed', '#0891b2', '#475569',
];

interface StatsCardProps {
  label: string;
  value: number | string;
  sub?: string;
}

function StatsCard({ label, value, sub }: StatsCardProps) {
  return (
    <Card>
      <CardContent className="pt-6">
        <p className="text-2xl font-bold">{typeof value === 'number' ? formatNumber(value) : value}</p>
        <p className="text-sm font-medium">{label}</p>
        {sub && <p className="text-xs text-muted-foreground">{sub}</p>}
      </CardContent>
    </Card>
  );
}

export function ProjectStatsOverview({ stats }: { stats: ProjectStats }) {
  const yearData = Object.entries(stats.papers_by_year)
    .map(([year, count]) => ({ year, count }))
    .sort((a, b) => a.year.localeCompare(b.year));

  const omicData = Object.entries(stats.datasets_by_omic_type)
    .map(([name, value]) => ({ name, value }));

  return (
    <div className="space-y-6">
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        <StatsCard label="Total Papers" value={stats.total_papers} />
        <StatsCard label="Included" value={stats.included_papers} sub="papers" />
        <StatsCard label="Total Datasets" value={stats.total_datasets} />
        <StatsCard label="Total Samples" value={stats.total_samples} />
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
        <Card>
          <CardHeader>
            <CardTitle className="text-sm">Papers by Year</CardTitle>
          </CardHeader>
          <CardContent>
            <ResponsiveContainer width="100%" height={200}>
              <BarChart data={yearData}>
                <XAxis dataKey="year" tick={{ fontSize: 11 }} />
                <YAxis tick={{ fontSize: 11 }} />
                <Tooltip />
                <Bar dataKey="count" fill="#0c93e7" />
              </BarChart>
            </ResponsiveContainer>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="text-sm">Datasets by Omic Type</CardTitle>
          </CardHeader>
          <CardContent>
            <ResponsiveContainer width="100%" height={200}>
              <PieChart>
                <Pie data={omicData} dataKey="value" nameKey="name" cx="50%" cy="50%" outerRadius={70} label>
                  {omicData.map((_, index) => (
                    <Cell key={index} fill={OMIC_COLORS[index % OMIC_COLORS.length]} />
                  ))}
                </Pie>
                <Tooltip />
              </PieChart>
            </ResponsiveContainer>
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
