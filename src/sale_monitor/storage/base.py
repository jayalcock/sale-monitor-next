class StorageBase:
    """Base class for storage backends."""

    def load(self):
        """Load data from the storage."""
        raise NotImplementedError("Load method must be implemented by subclasses.")

    def save(self, data):
        """Save data to the storage."""
        raise NotImplementedError("Save method must be implemented by subclasses.")

    def delete(self, key):
        """Delete a specific item from the storage."""
        raise NotImplementedError("Delete method must be implemented by subclasses.")

    def clear(self):
        """Clear all data from the storage."""
        raise NotImplementedError("Clear method must be implemented by subclasses.")