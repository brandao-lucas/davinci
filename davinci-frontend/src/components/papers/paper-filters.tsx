'use client';

import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Checkbox } from '@/components/ui/checkbox';
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from '@/components/ui/select';
import type { PaperFilters } from '@/lib/types/paper';

interface PaperFiltersProps {
  filters: PaperFilters;
  onChange: (filters: PaperFilters) => void;
}

const STATUSES = ['pending', 'included', 'excluded', 'maybe'];

const PUB_TYPES = [
  'Journal Article',
  'Review',
  'Systematic Review',
  'Meta-Analysis',
  'Randomized Controlled Trial',
  'Clinical Trial',
  'Observational Study',
  'Case Reports',
  'Editorial',
  'Letter',
  'Comment',
];

export function PaperFiltersPanel({ filters, onChange }: PaperFiltersProps) {
  return (
    <div className="space-y-4">
      <div className="space-y-1.5">
        <Label>Status</Label>
        <Select
          value={filters.curation_status ?? ''}
          onValueChange={(v) => onChange({ ...filters, curation_status: v === 'all' ? undefined : v })}
        >
          <SelectTrigger>
            <SelectValue placeholder="All statuses" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">All</SelectItem>
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

      <div className="space-y-1.5">
        <Label>Publication type</Label>
        <Select
          value={filters.pub_type ?? 'all'}
          onValueChange={(v) => onChange({ ...filters, pub_type: v === 'all' ? undefined : v })}
        >
          <SelectTrigger>
            <SelectValue placeholder="All types" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">All types</SelectItem>
            {PUB_TYPES.map((t) => (
              <SelectItem key={t} value={t}>{t}</SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>

      <div className="space-y-2 pt-1">
        <Label className="text-xs uppercase tracking-wide text-muted-foreground">Content</Label>
        <label className="flex items-center gap-2 cursor-pointer text-sm">
          <Checkbox
            checked={!!filters.has_abstract}
            onCheckedChange={(v) => onChange({ ...filters, has_abstract: v ? true : undefined })}
          />
          With abstract
        </label>
        <label className="flex items-center gap-2 cursor-pointer text-sm">
          <Checkbox
            checked={!!filters.free_full_text}
            onCheckedChange={(v) => onChange({ ...filters, free_full_text: v ? true : undefined })}
          />
          Free full text
        </label>
      </div>
    </div>
  );
}
