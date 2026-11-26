//! Live geometry and runtime layout diagnostics for the X11 presentation.

use super::*;

pub(crate) fn dump_live_layout_json(
    width: i32,
    height: i32,
    text_scale: f32,
) -> Result<serde_json::Value, String> {
    let display = unsafe { XOpenDisplay(ptr::null()) };
    if display.is_null() {
        return Err("could not open an X11 display for live layout metrics".to_owned());
    }
    let screen = unsafe { XDefaultScreen(display) };
    let visual = unsafe { XDefaultVisual(display, screen) };
    if visual.is_null() {
        unsafe { XCloseDisplay(display) };
        return Err("X11 display has no compatible visual for live layout metrics".to_owned());
    }
    let root = unsafe { XRootWindow(display, screen) };
    let window_move_strategy = select_window_move_strategy(display, screen);
    let window_move_telemetry = WindowMoveTelemetry::for_strategy(window_move_strategy);
    let colormap = unsafe { XDefaultColormap(display, screen) };
    let mut window_attributes = XSetWindowAttributes {
        background_pixmap: 0,
        background_pixel: 0,
        border_pixmap: 0,
        border_pixel: 0,
        bit_gravity: 0,
        win_gravity: 0,
        backing_store: 0,
        backing_planes: 0,
        backing_pixel: 0,
        save_under: 0,
        event_mask: 0,
        do_not_propagate_mask: 0,
        override_redirect: 0,
        colormap,
        cursor: 0,
    };
    let window = unsafe {
        XCreateWindow(
            display,
            root,
            0,
            0,
            width.max(1) as CUint,
            height.max(1) as CUint,
            0,
            XDefaultDepth(display, screen),
            INPUT_OUTPUT as CUint,
            visual,
            CW_COLORMAP,
            &mut window_attributes,
        )
    };
    if window == 0 {
        unsafe { XCloseDisplay(display) };
        return Err("could not create an X11 drawable for live layout metrics".to_owned());
    }
    let result = (|| {
        let text = TextRenderer::new(display, screen, window, visual, colormap, text_scale)?;
        let metrics = UiFontMetrics {
            body: text.metrics_for(FontRole::Body),
            input: text.metrics_for(FontRole::Input),
            heading: text.metrics_for(FontRole::Heading),
            instrument: text.metrics_for(FontRole::Instrument),
        };
        let layout = FrameGeometry::for_window(width, height, metrics);
        let prompt = PromptLayout::from_rect(
            layout.prompt,
            if layout.compact {
                metrics.instrument
            } else {
                metrics.input
            },
            metrics.instrument,
            layout.prompt_padding_y,
            layout.prompt_gap,
            layout.prompt_bottom_padding,
            !layout.compact,
        );
        let mut dump = serde_json::to_value(layout).map_err(|error| error.to_string())?;
        let object = dump
            .as_object_mut()
            .ok_or_else(|| "native layout did not serialize as an object".to_owned())?;
        object.insert("metrics_source".to_owned(), serde_json::json!("live_xft"));
        object.insert("text_scale".to_owned(), serde_json::json!(text_scale));
        object.insert("metrics".to_owned(), serde_json::to_value(metrics).unwrap());
        object.insert(
            "font_pixel_sizes".to_owned(),
            serde_json::to_value(text.font_pixel_sizes()).unwrap(),
        );
        object.insert(
            "terminal_size".to_owned(),
            serde_json::to_value(layout.terminal_size()).unwrap(),
        );
        object.insert(
            "prompt_layout".to_owned(),
            serde_json::to_value(prompt).unwrap(),
        );
        object.insert(
            "window_management".to_owned(),
            window_management_diagnostics(
                display,
                screen,
                window_move_strategy,
                window_move_telemetry,
            ),
        );
        Ok(dump)
    })();
    unsafe {
        XDestroyWindow(display, window);
        XCloseDisplay(display);
    }
    result
}

pub(crate) fn write_runtime_layout_dump(
    display: *mut Display,
    screen: c_int,
    width: i32,
    height: i32,
    metrics: UiFontMetrics,
    font_pixel_sizes: [i32; 4],
    text_scale: f32,
    configure_events: u64,
    window_move_strategy: WindowMoveStrategy,
    window_move_telemetry: WindowMoveTelemetry,
) {
    let Ok(path) = env::var("ATHENA_NATIVE_LAYOUT_DUMP") else {
        return;
    };
    let layout = FrameGeometry::for_window(width, height, metrics);
    let prompt = PromptLayout::from_rect(
        layout.prompt,
        if layout.compact {
            metrics.instrument
        } else {
            metrics.input
        },
        metrics.instrument,
        layout.prompt_padding_y,
        layout.prompt_gap,
        layout.prompt_bottom_padding,
        !layout.compact,
    );
    let mut value = match serde_json::to_value(layout) {
        Ok(value) => value,
        Err(error) => {
            eprintln!("could not serialize native layout dump: {error}");
            return;
        }
    };
    let Some(object) = value.as_object_mut() else {
        eprintln!("could not serialize native layout dump as an object");
        return;
    };
    object.insert("metrics_source".to_owned(), serde_json::json!("live_xft"));
    object.insert(
        "metrics".to_owned(),
        serde_json::to_value(metrics).expect("font metrics serialize"),
    );
    object.insert(
        "font_pixel_sizes".to_owned(),
        serde_json::to_value(font_pixel_sizes).expect("font sizes serialize"),
    );
    object.insert("text_scale".to_owned(), serde_json::json!(text_scale));
    object.insert(
        "terminal_size".to_owned(),
        serde_json::to_value(layout.terminal_size()).expect("terminal size serialize"),
    );
    object.insert(
        "configure_events".to_owned(),
        serde_json::json!(configure_events),
    );
    object.insert(
        "prompt_layout".to_owned(),
        serde_json::to_value(prompt).expect("prompt layout serialize"),
    );
    object.insert(
        "window_management".to_owned(),
        window_management_diagnostics(display, screen, window_move_strategy, window_move_telemetry),
    );
    if let Err(error) = std::fs::write(path, value.to_string()) {
        eprintln!("could not write native layout dump: {error}");
    }
}
