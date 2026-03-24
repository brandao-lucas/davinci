import { create } from 'zustand';
import type { PaperFilters } from '@/lib/types/paper';
import type { DatasetFilters } from '@/lib/types/dataset';

interface FilterState {
  paperFilters: Record<string, PaperFilters>;
  datasetFilters: Record<string, DatasetFilters>;
  setPaperFilters: (projectId: string, filters: PaperFilters) => void;
  setDatasetFilters: (projectId: string, filters: DatasetFilters) => void;
  clearPaperFilters: (projectId: string) => void;
  clearDatasetFilters: (projectId: string) => void;
}

export const useFilterStore = create<FilterState>((set) => ({
  paperFilters: {},
  datasetFilters: {},
  setPaperFilters: (projectId, filters) =>
    set((state) => ({
      paperFilters: { ...state.paperFilters, [projectId]: filters },
    })),
  setDatasetFilters: (projectId, filters) =>
    set((state) => ({
      datasetFilters: { ...state.datasetFilters, [projectId]: filters },
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
}));
