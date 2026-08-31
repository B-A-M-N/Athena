use super::*;

pub(crate) struct Clipboard {
    clipboard: Atom,
    primary: Atom,
    utf8_string: Atom,
    string: Atom,
    targets: Atom,
    atom: Atom,
    property: Atom,
    text: String,
}

impl Clipboard {
    pub(crate) fn new(display: *mut Display) -> Self {
        Self {
            clipboard: intern_atom(display, "CLIPBOARD"),
            primary: intern_atom(display, "PRIMARY"),
            utf8_string: intern_atom(display, "UTF8_STRING"),
            string: intern_atom(display, "STRING"),
            targets: intern_atom(display, "TARGETS"),
            atom: intern_atom(display, "ATOM"),
            property: intern_atom(display, "ATHENA_SELECTION"),
            text: String::new(),
        }
    }

    pub(crate) fn own(&mut self, display: *mut Display, window: Window, text: String) {
        self.text = text;
        unsafe {
            XSetSelectionOwner(display, self.primary, window, CURRENT_TIME);
            XSetSelectionOwner(display, self.clipboard, window, CURRENT_TIME);
        }
    }

    pub(crate) fn request(&self, display: *mut Display, window: Window) {
        unsafe {
            XConvertSelection(
                display,
                self.clipboard,
                self.utf8_string,
                self.property,
                window,
                CURRENT_TIME,
            );
            XFlush(display);
        }
    }

    pub(crate) fn handle_event(
        &mut self,
        display: *mut Display,
        window: Window,
        event: &mut XEvent,
    ) -> Option<Vec<u8>> {
        match event.type_ {
            SELECTION_REQUEST => {
                let request =
                    unsafe { &*((&mut *event) as *mut XEvent as *const XSelectionRequestEvent) };
                self.respond(display, request);
                None
            }
            SELECTION_NOTIFY => {
                let notification =
                    unsafe { &*((&mut *event) as *mut XEvent as *const XSelectionEvent) };
                if notification.requestor != window || notification.property == 0 {
                    return None;
                }
                let mut actual_type = 0;
                let mut actual_format = 0;
                let mut nitems = 0;
                let mut bytes_after = 0;
                let mut data: *mut u8 = ptr::null_mut();
                let status = unsafe {
                    XGetWindowProperty(
                        display,
                        window,
                        notification.property,
                        0,
                        1_048_576,
                        1,
                        self.utf8_string,
                        &mut actual_type,
                        &mut actual_format,
                        &mut nitems,
                        &mut bytes_after,
                        &mut data,
                    )
                };
                if status != 0 || actual_format != 8 || data.is_null() {
                    if !data.is_null() {
                        unsafe { XFree(data.cast()) };
                    }
                    return None;
                }
                let bytes = unsafe { std::slice::from_raw_parts(data, nitems as usize).to_vec() };
                unsafe {
                    XFree(data.cast());
                    XDeleteProperty(display, window, notification.property);
                }
                Some(bytes)
            }
            SELECTION_CLEAR => {
                self.text.clear();
                None
            }
            _ => None,
        }
    }

    fn respond(&self, display: *mut Display, request: &XSelectionRequestEvent) {
        let property = if request.property == 0 {
            request.target
        } else {
            request.property
        };
        let mut accepted = property;
        unsafe {
            if request.target == self.targets {
                let targets = [self.utf8_string, self.string, self.targets];
                XChangeProperty(
                    display,
                    request.requestor,
                    property,
                    self.atom,
                    32,
                    PROP_MODE_REPLACE,
                    targets.as_ptr().cast(),
                    targets.len() as c_int,
                );
            } else if request.target == self.utf8_string || request.target == self.string {
                XChangeProperty(
                    display,
                    request.requestor,
                    property,
                    request.target,
                    8,
                    PROP_MODE_REPLACE,
                    self.text.as_bytes().as_ptr(),
                    self.text.len().min(c_int::MAX as usize) as c_int,
                );
            } else {
                accepted = 0;
            }
            let response = XSelectionEvent {
                type_: SELECTION_NOTIFY,
                serial: 0,
                send_event: 1,
                display,
                requestor: request.requestor,
                selection: request.selection,
                target: request.target,
                property: accepted,
                time: request.time,
            };
            let mut event = XEvent {
                type_: 0,
                pad: [0; 24],
            };
            ptr::write(
                (&mut event as *mut XEvent).cast::<XSelectionEvent>(),
                response,
            );
            XSendEvent(display, request.requestor, 0, 0, &mut event);
            XFlush(display);
        }
    }
}
