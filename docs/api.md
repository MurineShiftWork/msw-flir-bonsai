# API reference

## msw_flir_bonsai.runner

Camera subprocess management: launch and stop Bonsai processes.

::: msw_flir_bonsai.runner.BonsaiCameraRunner

::: msw_flir_bonsai.runner.MultiCameraRunner

## msw_flir_bonsai.probe

Camera identity before launch: serials per SDK index, with a USB fallback.

::: msw_flir_bonsai.probe.probe_cameras

::: msw_flir_bonsai.probe.resolve_camera_units

::: msw_flir_bonsai.probe.ProbeResult

## msw_flir_bonsai.msw

MSW camera backend (`msw.camera` entry point `flir_bonsai`).

::: msw_flir_bonsai.msw.FlirBonsaiBackend

## msw_flir_bonsai.timestamps

Preprocessing utilities for Bonsai camera CSVs.

::: msw_flir_bonsai.timestamps.unwrap_cyclic

::: msw_flir_bonsai.timestamps.unwrap_counter

::: msw_flir_bonsai.timestamps.load_camera_csv

::: msw_flir_bonsai.timestamps.preprocess_camera_csv

::: msw_flir_bonsai.timestamps.detect_dropped_frames

## msw_flir_bonsai.alignment

Barcode and TTL alignment between camera frames and behaviour timestamps.

::: msw_flir_bonsai.alignment.extract_camera_barcodes

::: msw_flir_bonsai.alignment.align_barcodes

::: msw_flir_bonsai.alignment.align_ttl_edges

## msw_flir_bonsai.bonsai_workflows

::: msw_flir_bonsai.bonsai_workflows.workflow_path
