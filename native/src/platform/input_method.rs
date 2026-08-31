use super::*;

pub(crate) struct InputMethod {
    im: *mut Xim,
    pub(crate) ic: *mut Xic,
}

pub(crate) struct KeyLookup {
    pub(crate) keysym: c_ulong,
    pub(crate) bytes: Vec<u8>,
}

pub(crate) fn terminal_key_bytes(keysym: c_ulong, mode: TermMode, fallback: &[u8]) -> Vec<u8> {
    let arrow = |key: u8| {
        if mode.contains(TermMode::APP_CURSOR) {
            vec![0x1b, b'O', key]
        } else {
            vec![0x1b, b'[', key]
        }
    };
    match keysym {
        XK_LEFT => arrow(b'D'),
        XK_RIGHT => arrow(b'C'),
        XK_UP => arrow(b'A'),
        XK_DOWN => arrow(b'B'),
        XK_HOME => arrow(b'H'),
        XK_END => arrow(b'F'),
        XK_PAGE_UP => b"\x1b[5~".to_vec(),
        XK_PAGE_DOWN => b"\x1b[6~".to_vec(),
        XK_DELETE => b"\x1b[3~".to_vec(),
        XK_BACKSPACE => vec![0x7f],
        XK_TAB => vec![b'\t'],
        XK_RETURN => vec![b'\r'],
        _ => fallback.to_vec(),
    }
}

impl InputMethod {
    pub(crate) fn new(display: *mut Display, window: Window) -> Option<Self> {
        let im = unsafe { XOpenIM(display, ptr::null_mut(), ptr::null_mut(), ptr::null_mut()) };
        if im.is_null() {
            return None;
        }
        let input_style = CString::new("inputStyle").expect("static XIM attribute");
        let client_window = CString::new("clientWindow").expect("static XIM attribute");
        // XIMPreeditNothing | XIMStatusNothing. This lets the platform input
        // method compose Unicode/IME text without asking the renderer to own
        // composition state.
        let style: c_ulong = 0x0008 | 0x0400;
        let ic = unsafe {
            XCreateIC(
                im,
                input_style.as_ptr(),
                style,
                client_window.as_ptr(),
                window,
                ptr::null::<c_char>(),
            )
        };
        if ic.is_null() {
            unsafe { XCloseIM(im) };
            return None;
        }
        Some(Self { im, ic })
    }

    fn lookup(
        &mut self,
        event: *mut XKeyEvent,
        buffer: *mut c_char,
        length: c_int,
        keysym: *mut c_ulong,
        status: *mut c_int,
    ) -> c_int {
        unsafe { Xutf8LookupString(self.ic, event, buffer, length, keysym, status) }
    }
}

pub(crate) fn lookup_key(
    input_method: Option<&mut InputMethod>,
    event: &mut XKeyEvent,
) -> KeyLookup {
    if let Some(input_method) = input_method {
        let mut capacity = 128_usize;
        loop {
            let mut buffer = vec![0_i8; capacity];
            let mut keysym = 0 as c_ulong;
            let mut status = 0;
            let count = input_method.lookup(
                event,
                buffer.as_mut_ptr(),
                buffer.len().min(c_int::MAX as usize) as c_int,
                &mut keysym,
                &mut status,
            );
            if status == X_BUFFER_OVERFLOW {
                let required = usize::try_from(count).unwrap_or(capacity.saturating_mul(2));
                let next = required.max(capacity.saturating_mul(2));
                if next > MAX_XIM_BUFFER {
                    return KeyLookup {
                        keysym,
                        bytes: Vec::new(),
                    };
                }
                capacity = next;
                continue;
            }
            let length = bounded_lookup_length(count, buffer.len()).unwrap_or(0);
            return KeyLookup {
                keysym,
                bytes: buffer[..length].iter().map(|byte| *byte as u8).collect(),
            };
        }
    }

    let mut buffer = [0_i8; 128];
    let mut keysym = 0 as c_ulong;
    let mut compose = XComposeStatus {
        compose_ptr: ptr::null_mut(),
        chars_matched: 0,
    };
    let count = unsafe {
        XLookupString(
            event,
            buffer.as_mut_ptr(),
            buffer.len() as c_int,
            &mut keysym,
            &mut compose,
        )
    };
    let length = bounded_lookup_length(count, buffer.len()).unwrap_or(0);
    KeyLookup {
        keysym,
        bytes: buffer[..length].iter().map(|byte| *byte as u8).collect(),
    }
}

pub(crate) fn bounded_lookup_length(count: c_int, capacity: usize) -> Option<usize> {
    usize::try_from(count)
        .ok()
        .filter(|length| *length <= capacity)
}

impl Drop for InputMethod {
    fn drop(&mut self) {
        unsafe {
            XDestroyIC(self.ic);
            XCloseIM(self.im);
        }
    }
}
