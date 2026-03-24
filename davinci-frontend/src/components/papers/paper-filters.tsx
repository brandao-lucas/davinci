'use client';

import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from '@/components/ui/select';
import type { PaperFilters } from '@/lib/types/paper';

interface PaperFiltersProps {
  filters: PaperFilters;
  onChange: (filters: PaperFilters) => void;
}

const STATUSES = ['pending', 'included', 'excluded', 'maybe'];

export function PaperFiltersPanel({ filters, onChange }: PaperFiltersProps) {
  return (
    <div className="space-y-4">
      <div className="space-y-1.5">
        <Label>Status</Label>
        <Select
          value={filters.curation_status ?? ''}
          onValueChange={(v) => onChange({ ...filters, curation_status: v || undefined })}
        >
          <SelectTrigger>
            <SelectValue placeholder="All statuses" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="">All</SelectItem>
            {STATUSES.map((s) => (
              <SelectItem key={s} value={s}>{s}</SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>

      <div className="flex gap-2">
        <div className="flex-1 space-y-1.5">
          <Label>Year from</Label>
          <Input
            type="number"
            placeholder="1990"
            value={filters.pub_year_min ?? ''}
            onChange={(e) => onChange({ ...filters, pub_year_min: e.target.value ? Number(e.target.value) : undefined })}
          />
        </div>
        <div className="flex-1 space-y-1.5">
          <Label>Year to</Label>
          <Input
            type="number"
            placeholder="2024"
            value={filters.pub_year_max ?? ''}
            onChange={(e) => onChange({ ...filters, pub_year_max: e.target.value ? Number(e.target.value) : undefined })}
          />
        </div>
      </div>

      <div className="space-y-1.5">
        <Label>Journal</Label>
        <Input
          placeholder="Nature, NEJM…"
          value={filters.journal ?? ''}
          onChange={(e) => onChange({ ...filters, journal: e.target.value || undefined })}
        />
      </div>
    </div>
  );
}
