import { create } from 'zustand';
import type { PaperFilters } from '@/lib/types/paper';
import type { DatasetFilters } from '@/lib/types/dataset';
import type { SampleFilters } from '@/lib/types/sample';

interface FilterState {
  paperFilters: Record<string, PaperFilters>;
  datasetFilters: Record<string, DatasetFilters>;
  sampleFilters: Record<string, SampleFilters>;
  setPaperFilters: (projectId: string, filters: PaperFilters) => void;
  setDatasetFilters: (projectId: string, filters: DatasetFilters) => void;
  setSampleFilters: (key: string, filters: SampleFilters) => void;
  clearPaperFilters: (projectId: string) => void;
  clearDatasetFilters: (projectId: string) => void;
  clearSampleFilters: (key: string) => void;
}

export const useFilterStore = create<FilterState>((set) => ({
  paperFilters: {},
  datasetFilters: {},
  sampleFilters: {},
  setPaperFilters: (projectId, filters) =>
    set((state) => ({
      paperFilters: { ...state.paperFilters, [projectId]: filters },
    })),
  setDatasetFilters: (projectId, filters) =>
    set((state) => ({
      datasetFilters: { ...state.datasetFilters, [projectId]: filters },
    })),
  setSampleFilters: (key, filters) =>
    set((state) => ({
      sampleFilters: { ...state.sampleFilters, [key]: filters },
    })),
  clearPaperFilters: (projectId) =>
    set((state) => {
      const { [projectId]: _, ...rest } = state.paperFilters;
      return { paperFilters: rest };
    }),
  clearDatasetFilters: (projectId) =>
    set((state) => {
      const { [projectId]: _, ...rest } = state.datasetFilters;
      return { datasetFilters: rest };
    }),
  clearSampleFilters: (key) =>
    set((state) => {
      const { [key]: _, ...rest } = state.sampleFilters;
      return { sampleFilters: rest };
    }),
}));
