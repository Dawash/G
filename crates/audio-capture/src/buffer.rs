/// Fixed-capacity circular ring buffer for audio samples.
/// When full, oldest samples are silently overwritten (pre-buffer semantics).
pub struct RingBuffer {
    data: Vec<f32>,
    capacity: usize,
    write_pos: usize,
    len: usize,
}

impl RingBuffer {
    pub fn new(capacity: usize) -> Self {
        let cap = capacity.max(1);
        Self {
            data: vec![0.0_f32; cap],
            capacity: cap,
            write_pos: 0,
            len: 0,
        }
    }

    /// Push samples, overwriting oldest entries when full.
    pub fn push_slice(&mut self, samples: &[f32]) {
        for &s in samples {
            self.data[self.write_pos] = s;
            self.write_pos = (self.write_pos + 1) % self.capacity;
            if self.len < self.capacity {
                self.len += 1;
            }
        }
    }

    /// Drain all contents in order (oldest first). Resets the buffer.
    pub fn drain_to_vec(&mut self) -> Vec<f32> {
        if self.len == 0 {
            return Vec::new();
        }
        let mut out = Vec::with_capacity(self.len);
        // If buffer is not full, data starts at index 0.
        // If buffer is full, the oldest entry is at write_pos.
        let start = if self.len < self.capacity {
            0
        } else {
            self.write_pos
        };
        for i in 0..self.len {
            out.push(self.data[(start + i) % self.capacity]);
        }
        self.len = 0;
        self.write_pos = 0;
        out
    }

    pub fn len(&self) -> usize {
        self.len
    }

    pub fn is_empty(&self) -> bool {
        self.len == 0
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_push_and_drain() {
        let mut rb = RingBuffer::new(6);
        rb.push_slice(&[1.0, 2.0, 3.0]);
        let v = rb.drain_to_vec();
        assert_eq!(v, vec![1.0, 2.0, 3.0]);
        assert_eq!(rb.len(), 0);
    }

    #[test]
    fn test_overwrite_oldest() {
        let mut rb = RingBuffer::new(3);
        rb.push_slice(&[1.0, 2.0, 3.0, 4.0]); // 4th overwrites 1st
        let v = rb.drain_to_vec();
        assert_eq!(v, vec![2.0, 3.0, 4.0]);
    }

    #[test]
    fn test_drain_empty() {
        let mut rb = RingBuffer::new(4);
        assert_eq!(rb.drain_to_vec(), vec![]);
    }
}
