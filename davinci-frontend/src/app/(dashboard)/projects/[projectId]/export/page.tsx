'use client';

import { use, useState } from 'react';
import { PageHeader } from '@/components/layout/page-header';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { projectsApi } from '@/lib/api/projects';
import { saveFile } from '@/lib/utils/export';
import { Download } from 'lucide-react';

export default function ExportPage({ params }: { params: Promise<{ projectId: string }> }) {
  const { projectId } = use(params);
  const [loading, setLoading] = useState<string | null>(null);

  const handleExport = async (format: 'json' | 'csv') => {
    setLoading(format);
    try {
      const response = await projectsApi.exportData(projectId, format);
      const data = format === 'json'
        ? JSON.stringify(response.data, null, 2)
        : (response.data as string);
      await saveFile(data, `davinci-export.${format}`);
    } finally {
      setLoading(null);
    }
  };

  return (
    <div className="space-y-6 max-w-2xl">
      <PageHeader title="Export" description="Download your curated data" />

      <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
        <Card>
          <CardHeader>
            <CardTitle className="text-base">JSON Export</CardTitle>
            <CardDescription>Full structured export with papers, datasets and links</CardDescription>
          </CardHeader>
          <CardContent>
            <Button
              className="w-full"
              onClick={() => handleExport('json')}
              disabled={loading === 'json'}
            >
              <Download className="h-4 w-4 mr-2" />
              {loading === 'json' ? 'Exporting…' : 'Download JSON'}
            </Button>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="text-base">CSV Export</CardTitle>
            <CardDescription>Included papers as a flat CSV spreadsheet</CardDescription>
          </CardHeader>
          <CardContent>
            <Button
              className="w-full"
              variant="outline"
              onClick={() => handleExport('csv')}
              disabled={loading === 'csv'}
            >
              <Download className="h-4 w-4 mr-2" />
              {loading === 'csv' ? 'Exporting…' : 'Download CSV'}
            </Button>
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
