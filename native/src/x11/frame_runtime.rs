//! Frame cadence and presentation ownership for the X11 window runtime.

use super::*;

pub(crate) struct FrameDrawContext<'a> {
    pub(crate) display: *mut Display,
    pub(crate) width: i32,
    pub(crate) height: i32,
    pub(crate) core: &'a mut NativeTerminalCore,
    pub(crate) projection: &'a mut Projection,
    pub(crate) selection: Option<((usize, usize), (usize, usize))>,
    pub(crate) text: &'a TextRenderer,
    pub(crate) focused: bool,
    pub(crate) input_buffer: &'a InputBuffer,
    pub(crate) options: &'a RendererOptions,
    pub(crate) presentation: PresentationSettings,
    pub(crate) stencil_available: bool,
    pub(crate) oi_target: &'a crate::render::oi::OiTarget,
    pub(crate) chassis_material: &'a crate::render::chassis::ChassisMaterial,
    pub(crate) presentation_surface: &'a mut crate::platform::PresentationSurface,
    pub(crate) metrics: UiFontMetrics,
    pub(crate) dirty: DirtyDomains,
    pub(crate) activity_dirty: bool,
}

pub(crate) struct FrameDrawResult {
    pub(crate) request_another_frame: bool,
}

pub(crate) struct FrameRuntime {
    presentation_clock: PresentationClock,
    benchmark_limit: Option<u64>,
    benchmark_done: bool,
    oi_dumped: bool,
    render_started: Instant,
    cpu_started: Option<f64>,
    last_draw: Option<Instant>,
    redraws: u64,
    initial_redraws: u64,
    event_redraws: u64,
    idle_redraws: u64,
    presented_frame_sequence: u64,
}

impl FrameRuntime {
    pub(crate) fn from_options(options: &RendererOptions) -> Result<Self, String> {
        Ok(Self {
            presentation_clock: PresentationClock::from_environment()?,
            benchmark_limit: options.benchmark_frames,
            benchmark_done: false,
            oi_dumped: false,
            render_started: Instant::now(),
            cpu_started: process_cpu_seconds(),
            last_draw: None,
            redraws: 0,
            initial_redraws: 0,
            event_redraws: 0,
            idle_redraws: 0,
            presented_frame_sequence: 0,
        })
    }

    pub(crate) fn last_draw(&self) -> Option<Instant> {
        self.last_draw
    }

    pub(crate) fn benchmark_done(&self) -> bool {
        self.benchmark_done
    }

    pub(crate) fn draw_if_ready(
        &mut self,
        now: Instant,
        context: FrameDrawContext<'_>,
    ) -> Option<FrameDrawResult> {
        let draw_ready = self
            .last_draw
            .map(|last| now.duration_since(last) >= crate::platform::active_frame_interval())
            .unwrap_or(true);
        if !(context.dirty.full || context.dirty.terminal || context.dirty.oi_motion) || !draw_ready
        {
            return None;
        }

        let presentation_time = self
            .presentation_clock
            .seconds_at_frame(self.presented_frame_sequence);
        if context.options.animations
            && !context.options.reduced_motion
            && projection_is_animated(context.projection)
        {
            context
                .projection
                .advance_animation(crate::platform::active_frame_interval().as_secs_f32());
        }
        crate::render::frame::draw_frame(crate::render::frame::FrameContext {
            width: context.width,
            height: context.height,
            core: context.core,
            projection: context.projection,
            selection: context.selection,
            text: context.text,
            focused: context.focused,
            input_buffer: context.input_buffer,
            options: context.options,
            presentation: context.presentation,
            stencil_available: context.stencil_available,
            oi_target: context.oi_target,
            chassis_material: context.chassis_material,
            dirty: context.dirty,
            effect_phase: if context.options.animations && !context.options.reduced_motion {
                context.projection.animation_phase(presentation_time)
            } else {
                0.0
            },
            motion_time: presentation_time,
        });
        let geometry = FrameGeometry::for_window(context.width, context.height, context.metrics);
        let mut present_regions = Vec::with_capacity(2);
        if context.dirty.full {
            present_regions.push(PixelRect {
                x: 0.0,
                y: 0.0,
                width: context.width as f32,
                height: context.height as f32,
            });
        } else {
            if context.dirty.terminal {
                present_regions.push(geometry.operator_inner);
            }
            if context.dirty.oi_motion {
                present_regions.push(geometry.oi_inner);
            }
        }
        context
            .presentation_surface
            .present_regions(&present_regions);
        crate::render::frame::draw_text_layer(crate::render::frame::TextLayerContext {
            display: context.display,
            width: context.width,
            height: context.height,
            core: context.core,
            projection: context.projection,
            text: context.text,
            focused: context.focused,
            input_buffer: context.input_buffer,
            options: context.options,
            dirty: context.dirty,
        });

        let capture_sync_configured = env::var("ATHENA_NATIVE_PRESENTATION_SYNC").is_ok()
            || env::var("ATHENA_NATIVE_LAYOUT_DUMP").is_ok()
            || env::var("ATHENA_NATIVE_OI_DUMP").is_ok();
        if capture_sync_configured {
            unsafe { XSync(context.display, 0) };
        }
        self.presented_frame_sequence = self.presented_frame_sequence.saturating_add(1);
        write_presentation_sync(
            self.presented_frame_sequence,
            context.width,
            context.height,
            context.projection,
        );
        if !self.oi_dumped {
            if let Ok(path) = env::var("ATHENA_NATIVE_OI_DUMP") {
                if let Err(error) = crate::render::oi::dump_framebuffer(context.oi_target, &path) {
                    eprintln!("could not write OI framebuffer dump: {error}");
                }
                self.oi_dumped = true;
            }
        }
        self.redraws += 1;
        if self.last_draw.is_none() {
            self.initial_redraws += 1;
        } else if context.activity_dirty {
            self.event_redraws += 1;
        } else {
            self.idle_redraws += 1;
        }
        self.last_draw = Some(now);

        let request_another_frame = if let Some(limit) = self.benchmark_limit {
            if self.redraws >= limit {
                self.benchmark_done = true;
                false
            } else {
                true
            }
        } else {
            false
        };
        Some(FrameDrawResult {
            request_another_frame,
        })
    }

    pub(crate) fn finish(&mut self, steady_elapsed_seconds: f64, steady_cpu_seconds: f64) {
        write_render_stats(crate::platform::RenderStatsContext {
            started: &self.render_started,
            cpu_started: self.cpu_started,
            steady_elapsed_seconds,
            steady_cpu_seconds,
            counters: crate::platform::RenderCounters {
                redraws: self.redraws,
                initial_redraws: self.initial_redraws,
                event_redraws: self.event_redraws,
                idle_redraws: self.idle_redraws,
                presented_frame_sequence: self.presented_frame_sequence,
            },
        });
    }
}
